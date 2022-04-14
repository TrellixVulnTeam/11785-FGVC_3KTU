from Models.ResNet import resnet50
from Models.ViT import vit_s16
from Dataset import create_dataloader
from running import setup4training, train_one_epoch, val_one_epoch
from utils import get_parameter_num
from configs import config, update_cfg, preprocess_cfg
from log import setup_default_logging

from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy
from timm.utils import ModelEma
from timm.data.mixup import Mixup

import os
import argparse
import yaml
import logging
import random
import numpy as np
import torch
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel
logger = logging.getLogger('train')

def parse_argument():

    parser = argparse.ArgumentParser(description='FGVC params')
    parser.add_argument('--config', type=str, default='')
    parser.add_argument('--local_rank', type=int, default=0)

    return parser.parse_known_args()


def create_model(config):

    name = 'resnet50'
    num_classes = 200
    pretrain = config.model.pretrained.pretrain
    pretrained_path = config.model.pretrained.path

    if name == 'resnet50':
        if pretrain:
            model = resnet50(pretrained=True)
            model.fc = nn.Linear(2048, num_classes)
        else:
            model = resnet50(pretrained=False)
            model.fc = nn.Linear(2048, num_classes)

    elif name == 'vit_s16':
        if pretrain:
            model = vit_s16(config, pretrained=True)
            model.head = nn.Linear(384, num_classes)
        else:
            model = vit_s16(config, pretrained=False)
            model.head = nn.Linear(384, num_classes)

    else:
        raise NotImplementedError

    return model
def main():
    setup_default_logging()
    args, config_overrided = parse_argument()
    print(args.config)
    update_cfg(config, args.config, config_overrided)
    preprocess_cfg(config, args.local_rank)

    if 'WORLD_SIZE' in os.environ:
        config.distributed = int(os.environ['WORLD_SIZE']) > 1
    else:
        config.distributed = False
    
    if config.distributed:
        config.device = 'cuda:%d' % config.local_rank
        torch.cuda.set_device(config.local_rank)
        torch.distributed.init_process_group(backend='nccl', init_method='env://')
        config.world_size = torch.distributed.get_world_size()
        config.rank = torch.distributed.get_rank()
        logger.info('Training in distributed mode with \
                     multiple processes, 1 GPU per process.\
                     Process {:d}, total {:d}.'.format(config.rank, config.world_size))
    else:
        config.device = 'cuda:0'
        config.world_size = 1
        config.rank = 0
        logger.info('Training with a single process on 1 GPU.')

    torch.manual_seed(config.seed + config.rank)
    np.random.seed(config.seed + config.rank)
    random.seed(config.seed + config.rank)
    

    model = create_model(config)
    model.cuda() 
    
    #-----------------------------------------------------------------------------------------------------------
    #Section for mixup & cutmix: 2 in 1 easy game
    mixup_fn = None
    mixup_active = config.train.mixup > 0 or config.train.cutmix > 0. or config.train.cutmix_minmax is not None
    if mixup_active:
        print("Mixup is activated!")
        mixup_fn = Mixup(
            mixup_alpha=config.train.mixup, cutmix_alpha=config.train.cutmix, cutmix_minmax=config.train.cutmix_minmax,
            prob=config.train.mixup_prob, switch_prob=config.train.mixup_switch_prob, mode=config.train.mixup_mode,
            label_smoothing=config.train.smoothing, num_classes=config.model.num_classes)
            
    if mixup_fn is not None:
        #smoothing is handled with mixup label transform
        criterion = SoftTargetCrossEntropy()
    elif config.train.smoothing > 0.:
        criterion = LabelSmoothingCrossEntropy(smoothing=config.train.smoothing)
    else:
        criterion = torch.nn.CrossEntropyLoss()

    print("criterion = %s" % str(criterion))
    
    #-----------------------------------------------------------------------------------------------------------

    #-----------------------------------------------------------------------------------------------------------
    ##Section for ema:
    model_ema = None
    if config.train.model_ema:
        # Important to create EMA model after cuda(), DP wrapper, and AMP but before SyncBN and DDP wrapper
        model_ema = ModelEma(
            model,
            decay=config.train.model_ema_decay,
            device='cpu' if config.train.ema_force_cpu else '',
            resume='')
        print("Using EMA with decay = %.8f" % config.train.model_ema_decay)    
    #-----------------------------------------------------------------------------------------------------------
    

    train_loader, val_loader = create_dataloader(config,logger)
    
    config.data.batches = len(train_loader)

    optimizer, scheduler, scaler = setup4training(model, config)

    num_epochs = config.optim.epochs + config.optim.warmup_epochs

    if config.distributed:
        model = DistributedDataParallel(model, device_ids=[config.local_rank])

    output_dir = None
    eval_metric = config.eval_metric
    best_metric = None
    best_epoch = None
    checkpoint_saver = None

    if config.local_rank == 0:
        output_dir = config.output_dir
        with open(os.path.join(config.output_dir, 'config.yaml'), 'w') as f:
            yaml.dump(config.to_dict(), f)

    for epoch in range(num_epochs):

        if config.distributed and hasattr(train_loader.sampler, 'set_epoch'):
            train_loader.sampler.set_epoch(epoch)
        train_metrics = train_one_epoch(epoch, model, train_loader,
                                        optimizer, criterion, scheduler,
                                        scaler, config,logger,mixup_fn = mixup_fn,model_ema=model_ema)
        eval_metrics = val_one_epoch(model, val_loader, config,logger)



if __name__ == "__main__":
    main() 
