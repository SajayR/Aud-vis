import torch
from torch.utils.data import DataLoader
from pathlib import Path
import logging
from tqdm import tqdm
import wandb
import torch.nn as nn
import torchvision.transforms as transforms
from model import AudioVisualModel
from dataset import AudioVisualDataset, VideoBatchSampler
from viz import AudioVisualizer
import numpy as np
import matplotlib.pyplot as plt
import psutil
import gc

class AudioVisualTrainer:
    def __init__(
        self,
        video_dir: str,
        output_dir: str,
        batch_size: int = 32,
        num_epochs: int = 100,
        learning_rate: float = 1e-4,
        num_workers: int = 12,
        vis_every: int = 250,
        num_vis_samples: int = 4,
        device: str = 'cuda',
        use_wandb: bool = True,
        force_new_training: bool = False  # New param to force fresh training
    ):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.vis_every = vis_every
        self.device = device
        self.use_wandb = use_wandb
        self.num_vis_samples = num_vis_samples
        self.model = AudioVisualModel().to(device)
        # Store hyperparameters in config
        self.config = {
            'batch_size': batch_size,
            'learning_rate': learning_rate,
            'num_epochs': num_epochs,
            'num_workers': num_workers,
            'vis_every': vis_every,
            'num_vis_samples': num_vis_samples
        }

        # Setup logging
        logging.basicConfig(
            filename=str(self.output_dir / 'training.log'),
            level=logging.INFO,
            format='%(asctime)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)

        # First, try to find latest checkpoint if not forcing new training
        self.start_epoch = 0
        self.global_step = 0
        self.best_loss = float('inf')

        if not force_new_training:
            checkpoint_path = self.find_latest_checkpoint()
            if checkpoint_path:
                print(f"Found checkpoint: {checkpoint_path}")
                self.load_checkpoint(checkpoint_path)
                # Update config with new hyperparameters while keeping training state
                print("Updating training configuration...")
                for key, value in self.config.items():
                    if key in locals():
                        print(f"Updating {key}: {self.config[key]} -> {locals()[key]}")
                        self.config[key] = locals()[key]

        # Initialize dataset and dataloader
        frame_transform = transforms.Compose([
            transforms.Resize((224, 224)),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], 
                              std=[0.229, 0.224, 0.225])
        ])

        self.dataset = AudioVisualDataset(
            video_dir=video_dir,
            frame_transform=frame_transform
        )

        self.batch_sampler = VideoBatchSampler(
            num_videos=len(self.dataset),
            batch_size=self.config['batch_size']
        )

        self.dataloader = DataLoader(
            self.dataset,
            batch_sampler=self.batch_sampler,
            num_workers=self.config['num_workers'],
            persistent_workers=True,
            worker_init_fn=lambda _: gc.collect()
        )

        # Initialize model and optimizer
        
        self.optimizer = torch.optim.AdamW(
            self.model.parameters(),
            lr=self.config['learning_rate']
        )

        # Learning rate scheduler - adjust for remaining epochs
        self.scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            self.optimizer,
            T_max=self.config['num_epochs'] - self.start_epoch
        )

        # Initialize visualizer
        self.visualizer = AudioVisualizer()

        # Initialize wandb
        if use_wandb:
            # Always initialize wandb, but with resume=True if we found a checkpoint
            wandb.init(
                project="audio-visual-learning",
                name=f"run_{Path(output_dir).stem}",
                config=self.config,
                resume=True if not force_new_training and self.find_latest_checkpoint() else False,
                id=None if force_new_training else wandb.util.generate_id()
            )

        # Save multiple random samples for visualization
        self.vis_samples = self._get_visualization_samples()

    def find_latest_checkpoint(self):
        """Find the latest checkpoint in the output directory"""
        checkpoints = list(self.output_dir.glob('checkpoint_epoch*.pt'))
        if not checkpoints:
            return None
        
        # Extract epoch and step numbers and find latest
        latest = max(checkpoints, key=lambda x: (
            int(str(x).split('epoch')[1].split('_')[0]),  # epoch number
            int(str(x).split('step')[1].split('.')[0])    # step number
        ))
        return latest

    def save_checkpoint(self, epoch: int, step: int):
        """Save a checkpoint with current configuration"""
        checkpoint_path = self.output_dir / f'checkpoint_epoch{epoch}_step{step}.pt'
        
        checkpoint = {
            'epoch': epoch,
            'step': step,
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'scheduler_state_dict': self.scheduler.state_dict(),
            'best_loss': self.best_loss,
            'config': self.config,
            'vis_samples': {  # Save visualization samples state
                'frames': self.vis_samples['frames'].cpu(),
                'mel_specs': self.vis_samples['mel_specs'].cpu(),
                'video_paths': self.vis_samples['video_paths']
            }
        }
        
        # Save with temp file to prevent corruption
        temp_path = checkpoint_path.with_suffix('.temp.pt')
        torch.save(checkpoint, temp_path)
        temp_path.rename(checkpoint_path)
        
        self.logger.info(f'Saved checkpoint to {checkpoint_path}')
        
        if self.use_wandb:
            wandb.save(str(checkpoint_path))

    def load_checkpoint(self, checkpoint_path: str):
        """Load a checkpoint and handle hyperparameter changes"""
        print(f"Loading checkpoint from {checkpoint_path}")
        checkpoint = torch.load(checkpoint_path)
        
        # Load model state
        self.model.load_state_dict(checkpoint['model_state_dict'])
        
        # Load training state
        self.start_epoch = checkpoint['epoch'] + 1
        self.global_step = checkpoint['step']
        self.best_loss = checkpoint['best_loss']
        
        # Load old config but don't override new settings yet
        self.config = checkpoint.get('config', self.config)
        
        # Optimizer needs to be initialized before loading state
        self.optimizer = torch.optim.AdamW(
            self.model.parameters(),
            lr=self.config['learning_rate']
        )
        self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        
        # Restore visualization samples if they exist
        if 'vis_samples' in checkpoint:
            self.vis_samples = {
                'frames': checkpoint['vis_samples']['frames'].to(self.device),
                'mel_specs': checkpoint['vis_samples']['mel_specs'].to(self.device),
                'video_paths': checkpoint['vis_samples']['video_paths']
            }
        
        print(f"Resumed from epoch {self.start_epoch} (step {self.global_step})")
        print(f"Best loss so far: {self.best_loss}")

    def _get_visualization_samples(self):
        """Get multiple random samples for visualization"""
        batch = next(iter(self.dataloader))
        indices = torch.randperm(len(batch['frame']))[:self.num_vis_samples]

        vis_samples = {
            'frames': batch['frame'][indices].to(self.device),
            'mel_specs': batch['mel_spec'][indices].to(self.device),
            'video_paths': [batch['video_path'][i] for i in indices]
        }

        return vis_samples

    def create_visualization(self, epoch: int, step: int):
        """Create visualizations for multiple samples with memory cleanup"""
        try:
            fig, axes = plt.subplots(self.num_vis_samples, 5, 
                                figsize=(20, 4*self.num_vis_samples))

            for i in range(self.num_vis_samples):
                self.visualizer.plot_attention_snapshot(
                    self.model,
                    self.vis_samples['frames'][i:i+1],
                    self.vis_samples['mel_specs'][i:i+1],
                    num_timesteps=5,
                    axes=axes[i] if self.num_vis_samples > 1 else axes
                )
                torch.cuda.empty_cache()

            if self.use_wandb:
                wandb.log({
                    "attention_snapshots": wandb.Image(plt),
                    "epoch": epoch,
                    "step": step
                })

            plt.close('all')

            # Only save videos every few epochs
            if epoch % 5 == 0:
                for i in range(self.num_vis_samples):
                    video_path = self.output_dir / f'attention_epoch{epoch}_sample{i}.mp4'
                    
                    torch.cuda.empty_cache()
                    gc.collect()

                    self.visualizer.make_attention_video(
                        self.model,
                        self.vis_samples['frames'][i:i+1],
                        self.vis_samples['mel_specs'][i:i+1],
                        video_path,
                        video_path=self.vis_samples['video_paths'][i]
                    )

                    if self.use_wandb:
                        wandb.log({
                            f"attention_video_{i}": wandb.Video(str(video_path)),
                            "epoch": epoch,
                            "step": step
                        })

                    torch.cuda.empty_cache()
                    gc.collect()

        finally:
            plt.close('all')
            torch.cuda.empty_cache()

    def train(self, num_epochs: int = None):
        """Train with support for extending training"""
        if num_epochs is not None:
            self.config['num_epochs'] = num_epochs
            # Adjust scheduler for new total epochs
            self.scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                self.optimizer,
                T_max=self.config['num_epochs'] - self.start_epoch
            )

        for epoch in range(self.start_epoch, self.config['num_epochs']):
            self.model.train()
            epoch_losses = []

            pbar = tqdm(self.dataloader, desc=f'Epoch {epoch}')
            for batch in pbar:
                self.model.train()
                frames = batch['frame'].to(self.device)
                audio = batch['mel_spec'].to(self.device)

                loss = self.model(frames, audio)

                self.optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
                self.optimizer.step()

                loss_value = loss.item()
                epoch_losses.append(loss_value)
                pbar.set_postfix({'loss': f'{loss_value:.4f}'})

                del frames, audio, loss
                torch.cuda.empty_cache()

                if self.global_step % 100 == 0:
                    gc.collect()

                if self.global_step % self.vis_every == 0:
                    with torch.no_grad():
                        self.create_visualization(epoch, self.global_step)
                    plt.close('all')
                    gc.collect()

                self.global_step += 1

            epoch_loss = np.mean(epoch_losses)
            self.logger.info(f'Epoch {epoch} - Loss: {epoch_loss:.4f}')

            if self.use_wandb:
                wandb.log({
                    'epoch_loss': epoch_loss,
                    'epoch': epoch,
                    'learning_rate': self.scheduler.get_last_lr()[0]
                })

            if epoch_loss < self.best_loss:
                self.best_loss = epoch_loss
                self.save_checkpoint(epoch, self.global_step)

            self.scheduler.step()

            # Regular checkpoint every 10 epochs
            if epoch % 10 == 0:
                self.save_checkpoint(epoch, self.global_step)

        print("Training completed!")

if __name__ == "__main__":
    # First training run
    trainer = AudioVisualTrainer(
        video_dir='/home/cisco/heyo/densefuck/VGGSound/vggsound_download/downloads',
        output_dir='./outputs',
        batch_size=8,
        num_epochs=500,
        learning_rate=1e-3,
        use_wandb=False
    )
    trainer.train()

    # Later, resume with different hyperparameters
    '''trainer = AudioVisualTrainer(
        video_dir='/home/cisco/heyo/densefuck/VGGSound/vggsound_download/downloads',
        output_dir='./outputs',
        batch_size=16,           # Changed batch size
        num_epochs=200,          # Extended training
        learning_rate=5e-5       # Lower learning rate
    )
    trainer.train()'''

    # Or force new training
    '''trainer = AudioVisualTrainer(
        video_dir='/home/cisco/heyo/densefuck/VGGSound/vggsound_download/downloads',
        output_dir='./outputs',
        batch_size=8,
        num_epochs=100,
        learning_rate=1e-4,
        force_new_training=True  # Ignore existing checkpoints
    )
    trainer.train()'''