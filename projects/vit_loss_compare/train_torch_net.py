import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.data as torch_data
from timm.data.constants import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
from timm.models.vision_transformer import vit_tiny_patch16_224
from torch.utils.data import Sampler
from torchvision import datasets, transforms

"""
Global Config
"""
BATCH_SIZE = 32
LR = 0.0001
BETAS = (0.9, 0.999)
WEIGHT_DECAY = 1e-8
TOTAL_STEPS = 1000
SAVA_FILE_PATH = "./torch_vit_tiny_loss.txt"


"""
Dataset, Sampler and Transforms Settings
"""


class CyclicSampler(Sampler):
    """This sampler supports cyclic sampling, and it is also compatible with
    non data parallelism and data parallelism.
    Arguments:
        dataset: dataset to be sampled.
        micro_batch_size: batch size for per model instance.
        global_batch_size is micro_batch_size times data_parallel_size.
        shuffle: whether to shuffle the dataset.
        consumed_samples: the number of samples that have been trained at the current time,
        used for resuming training.
        data_parallel_rank: local rank for data parallelism.
        data_parallel_size: the size of data parallelism.
        seed: random seed, used for reproducing experiments.
    """

    def __init__(
        self,
        dataset,
        micro_batch_size,
        shuffle=False,
        consumed_samples=0,
        data_parallel_rank=0,
        data_parallel_size=1,
        seed=0,
    ):
        self.dataset = dataset
        self.data_size = len(self.dataset)
        self.shuffle = shuffle

        self.data_parallel_rank = data_parallel_rank
        self.data_parallel_size = data_parallel_size
        self.micro_batch_size = micro_batch_size
        self.actual_batch_size = self.micro_batch_size * self.data_parallel_size
        self.data_size_per_epoch = self.data_size // self.actual_batch_size * self.micro_batch_size
        self.consumed_samples = consumed_samples

        self.seed = seed

    def __iter__(self):
        """divide the data into data_parallel_size buckets,
        and shuffle it if `shuffle` is set to `True`.
        Each processor samples from its own buckets and data_loader
        will load the corresponding data.
        """
        epoch = self.consumed_samples // self.data_size_per_epoch
        current_epoch_samples = self.consumed_samples % self.data_size_per_epoch
        batch = []

        while True:
            bucket_offset = current_epoch_samples // self.data_parallel_size
            start_idx = self.data_parallel_rank * self.data_size_per_epoch

            if self.shuffle:
                np.random.seed(0)
                random_idx = np.random.permutation(
                    self.data_size_per_epoch,
                ).tolist()
                indices = [start_idx + x for x in random_idx[bucket_offset:]]
            else:
                seq_idx = torch.arange(self.data_size_per_epoch).tolist()
                indices = [start_idx + x for x in seq_idx[bucket_offset:]]

            epoch += 1

            if hasattr(self.dataset, "supports_prefetch") and self.dataset.supports_prefetch:
                self.dataset.prefetch(indices)

            for idx in indices:
                batch.append(idx)
                if len(batch) == self.micro_batch_size:
                    self.consumed_samples += self.actual_batch_size
                    yield batch
                    batch = []

            current_epoch_samples = 0

    def __len__(self):
        return self.data_size

    def set_consumed_samples(self, consumed_samples):
        """you can recover the training iteration by setting `consumed_samples`."""
        self.consumed_samples = consumed_samples

    def set_epoch(self, epoch):
        """used for restoring training status."""
        self.epoch = epoch


no_aug_transform = transforms.Compose(
    [
        transforms.Resize((224, 224)),
        transforms.CenterCrop((224, 224)),
        transforms.ToTensor(),
        transforms.Normalize(mean=IMAGENET_DEFAULT_MEAN, std=IMAGENET_DEFAULT_STD),
    ]
)


imagenet_dataset = datasets.ImageFolder("/dataset/extract/train", transform=no_aug_transform)


train_loader = torch_data.DataLoader(
    imagenet_dataset,
    batch_sampler=CyclicSampler(
        dataset=imagenet_dataset, micro_batch_size=BATCH_SIZE, shuffle=True
    ),
)


"""
Training and Write Loss
"""
model = vit_tiny_patch16_224()
# 第一次执行需要保存一下权重, 之后可以把这个注释了只用一个权重
# torch.save(model.state_dict(), "./torch_vit_tiny_weight.pth")
model.eval()
model.cuda()

model.load_state_dict(
    torch.load("/home/rentianhe/code/OneFlow-Models/libai/vit_tiny_torch_weight.pth")
)
print("Successfully load model weights")
optimizer = torch.optim.AdamW(model.parameters(), lr=LR, betas=BETAS, weight_decay=WEIGHT_DECAY)
loss_func = nn.CrossEntropyLoss()


print("Start Training")
for idx, (data, target) in enumerate(train_loader):
    if idx == TOTAL_STEPS:
        break
    optimizer.zero_grad()
    data = data.cuda()
    target = target.cuda()
    output = model(data)
    loss = loss_func(output, target)
    print(f"Step: {idx}, Loss: {loss}")
    loss_print = loss.detach().cpu().numpy()
    with open(SAVA_FILE_PATH, "a") as f:
        f.write(str(loss_print))
        f.write("\n")
    loss.backward()
    optimizer.step()
print("Finish Training")
