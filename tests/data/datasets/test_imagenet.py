# coding=utf-8
# Copyright 2021 The OneFlow Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from libai.config import LazyConfig
from libai.data.datasets.imagenet import ImageNetDataset

train_set = ImageNetDataset("/DATA/disk1/ImageNet/extract", train=True)
assert len(train_set) == 1281167

test_set = ImageNetDataset("/DATA/disk1/ImageNet/extract", train=False)
assert len(test_set) == 50000
