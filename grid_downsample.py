import os

os.environ['OMP_NUM_THREADS'] = '1'
import argparse
import sys
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from color_distillation import datasets
import color_distillation.utils.transforms as T
from color_distillation import models
from color_distillation.trainer import CNNTrainer
from color_distillation.utils.draw_curve import draw_curve
from color_distillation.utils.logging import Logger


def main():
    # settings
    parser = argparse.ArgumentParser(description='Grid-wise down sample')
    parser.add_argument('--train', action='store_true', default=False)
    parser.add_argument('-d', '--dataset', type=str, default='cifar10')
    parser.add_argument('-a', '--arch', type=str, default='vgg16', choices=models.names())
    parser.add_argument('-j', '--num_workers', type=int, default=4)
    parser.add_argument('-b', '--batch_size', type=int, default=128, metavar='N',
                        help='input batch size for training (default: 128)')
    parser.add_argument('--epochs', type=int, default=20, metavar='N', help='number of epochs to train (default: 10)')
    parser.add_argument('--lr', type=float, default=0.1, metavar='LR', help='learning rate (default: 0.1)')
    parser.add_argument('--step_size', type=int, default=40)
    parser.add_argument('--weight_decay', type=float, default=5e-4)
    parser.add_argument('--momentum', type=float, default=0.5, metavar='M', help='SGD momentum (default: 0.5)')
    parser.add_argument('--seed', type=int, default=1, metavar='S', help='random seed (default: 1)')
    parser.add_argument('--log_interval', type=int, default=100, metavar='N',
                        help='how many batches to wait before logging training status')
    parser.add_argument('--sample_type', type=str, default='grid', choices=['grid', 'kmeans'])
    parser.add_argument('--downsample', type=float, default=1.0, help='down sample ratio for area')
    args = parser.parse_args()

    # seed
    np.random.seed(0)
    torch.manual_seed(0)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    if args.dataset == 'svhn':
        H, W, C = 32, 32, 3
    elif args.dataset == 'cifar10':
        H, W, C = 32, 32, 3
    else:
        raise Exception
    if args.sample_type == 'grid':
        sample_trans = [T.GridDownSample(args.downsample)]
        num_colors = None
    elif args.sample_type == 'kmeans':
        sample_trans = []
        num_colors = int(H * W * args.downsample)
    else:
        sample_trans = []
        num_colors = None

    # dataset
    if args.dataset == 'svhn':
        num_class = 10

        sampled_train_trans = T.Compose(sample_trans + [T.ToTensor(), T.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)), ])
        og_test_trans = T.Compose([T.ToTensor(), T.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)), ])
        sampled_test_trans = T.Compose(sample_trans + [T.ToTensor(), T.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)), ])

        sampled_train_set = datasets.SVHN('./data', split='train', download=True, transform=sampled_train_trans,
                                          num_colors=num_colors)
        og_test_set = datasets.SVHN('./data', split='test', download=True, transform=og_test_trans)
        sampled_test_set = datasets.SVHN('./data', split='test', download=True, transform=sampled_test_trans,
                                         num_colors=num_colors)
    elif args.dataset == 'cifar10':
        num_class = 10

        sampled_train_trans = T.Compose(sample_trans + [T.RandomCrop(32, padding=4),
                                                        T.RandomHorizontalFlip(), T.ToTensor(),
                                                        T.Normalize((0.4914, 0.4822, 0.4465),
                                                                    (0.2023, 0.1994, 0.2010)), ])
        og_test_trans = T.Compose([T.ToTensor(), T.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)), ])
        sampled_test_trans = T.Compose(sample_trans + [T.ToTensor(),
                                                       T.Normalize((0.4914, 0.4822, 0.4465),
                                                                   (0.2023, 0.1994, 0.2010)), ])

        sampled_train_set = datasets.CIFAR10(root='./data', train=True, download=True, transform=sampled_train_trans,
                                             num_colors=num_colors)
        og_test_set = datasets.CIFAR10(root='./data', train=False, download=True, transform=og_test_trans)
        sampled_test_set = datasets.CIFAR10(root='./data', train=False, download=True, transform=sampled_test_trans,
                                            num_colors=num_colors)
    else:
        raise Exception

    sampled_train_loader = torch.utils.data.DataLoader(sampled_train_set, batch_size=args.batch_size, shuffle=True,
                                                       num_workers=args.num_workers, pin_memory=True)
    og_test_loader = torch.utils.data.DataLoader(og_test_set, batch_size=args.batch_size, shuffle=False,
                                                 num_workers=args.num_workers, pin_memory=True)
    sampled_test_loader = torch.utils.data.DataLoader(sampled_test_set, batch_size=args.batch_size, shuffle=False,
                                                      num_workers=args.num_workers, pin_memory=True)

    logdir = 'logs/grid/{}/{}/downsample{}'.format(args.dataset, args.arch, args.downsample)
    if args.train:
        os.makedirs(logdir, exist_ok=True)
        sys.stdout = Logger(os.path.join(logdir, 'log.txt'), )
    print('Settings:')
    print(vars(args))

    # model
    model = models.create(args.arch, C, num_class).cuda()
    optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    # scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(optimizer, 10, 1, 0.01)
    scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr=args.lr,
                                                    steps_per_epoch=len(sampled_train_loader), epochs=args.epochs)

    # draw curve
    x_epoch = []
    train_loss_s = []
    train_prec_s = []
    og_test_loss_s = []
    og_test_prec_s = []
    masked_test_loss_s = []
    masked_test_prec_s = []

    trainer = CNNTrainer(model, nn.CrossEntropyLoss())

    # learn
    if args.train:
        for epoch in range(1, args.epochs + 1):
            print('Train on sampled dateset...')
            train_loss, train_prec = trainer.train(epoch, sampled_train_loader, optimizer, args.log_interval, scheduler)
            print('Test on original dateset...')
            og_test_loss, og_test_prec = trainer.test(og_test_loader)
            print('Test on sampled dateset...')
            masked_test_loss, masked_test_prec = trainer.test(sampled_test_loader)

            x_epoch.append(epoch)
            train_loss_s.append(train_loss)
            train_prec_s.append(train_prec)
            og_test_loss_s.append(og_test_loss)
            og_test_prec_s.append(og_test_prec)
            masked_test_loss_s.append(masked_test_loss)
            masked_test_prec_s.append(masked_test_prec)
            draw_curve(os.path.join(logdir, 'learning_curve.jpg'), x_epoch, train_loss_s, train_prec_s,
                       og_test_loss_s, og_test_prec_s, masked_test_loss_s, masked_test_prec_s)
        # save
        torch.save(model.state_dict(), os.path.join(logdir, 'model.pth'))
    else:
        logdir = 'logs/grid/{}/{}/downsample1.0'.format(args.dataset, args.arch)
        pretrain_dir = logdir + '/model.pth'
        model.load_state_dict(torch.load(pretrain_dir))
        model.eval()
        print('Test on original dateset...')
        trainer.test(og_test_loader)
        print('Test on sampled dateset...')
        trainer.test(sampled_test_loader)


if __name__ == '__main__':
    main()