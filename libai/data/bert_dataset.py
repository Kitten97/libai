# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

"""dataset for bert."""

import os
import math
import random
import collections
import numpy as np
import oneflow as flow

from .dataset_utils import get_samples_mapping


MaskedLmInstance = collections.namedtuple("MaskedLmInstance", ["index", "label"])

def is_start_piece(piece):
    """Check if the current word piece is the starting piece (BERT)."""
    # When a word has been split into
    # WordPieces, the first token does not have any marker and any subsequence
    # tokens are prefixed with ##. So whenever we see the ## token, we
    # append it to the previous set of word indexes.
    return not piece.startswith("##")

class BertDataset(flow.utils.data.Dataset):
    """
    Dataset containing sentence pairs for BERT training, Each index corresponds to a randomly generated sentence pair.
    """
    def __init__(self, name, indexed_dataset, tokenizer, data_prefix, num_epochs, max_num_samples, 
                 max_seq_length=512, mask_lm_prob=.15, max_preds_per_seq=None, short_seq_prob=.01, seed=1234, binary_head=True):
        self.name = name
        self.seed = seed
        self.mask_lm_prob = mask_lm_prob
        self.max_seq_length = max_seq_length
        self.binary_head = binary_head
        if max_preds_per_seq is None:
            max_preds_per_seq = math.ceil(max_seq_length * mask_lm_prob / 10) * 10
        self.max_preds_per_seq = max_preds_per_seq

        self.indexed_dataset = indexed_dataset

        self.samples_mapping = get_samples_mapping(self.indexed_dataset,
                                                   data_prefix,
                                                   num_epochs,
                                                   max_num_samples,
                                                   self.max_seq_length - 3, # account for added tokens
                                                   short_seq_prob,
                                                   self.seed,
                                                   self.name,
                                                   self.binary_head)
        
        self.tokenizer = tokenizer
        self.vocab_id_list = list(self.tokenizer.inv_vocab.keys())
        self.vocab_id_to_token_dict = tokenizer.inv_vocab
        self.cls_id = tokenizer.cls
        self.sep_id = tokenizer.sep
        self.mask_id = tokenizer.mask
        self.pad_id = tokenizer.pad
    
    def __len__(self):
        return self.samples_mapping.shape[0]
    
    def __getitem__(self, idx):
        start_idx, end_idx, seq_length = self.samples_mapping[idx]
        sample = [self.indexed_dataset[i] for i in range(start_idx, end_idx)]
        # Note that this rng state should be numpy and not python since
        # python randint is inclusive whereas the numpy one is exclusive.
        # We % 2 ** 32 since numpy requres the seed to be between 0 and 2 ** 32 - 1
        np_rng = np.random.RandomState(seed=((self.seed + idx) % 2 ** 32))
        return self.create_training_sample(sample, seq_length, np_rng)

    def create_training_sample(self, sample, target_seq_length, np_rng):
        if self.binary_head:
            # assume that we have at least two sentences in the sample
            assert len(sample) > 1
        assert target_seq_length <= self.max_seq_length

        # rivide sample into two segments (A and B).
        if self.binary_head:
            tokens_a, tokens_b, is_next_random = self.create_random_sentence_pair(sample, np_rng)
        else:
            tokens_a = []
            for j in range(len(sample)):
                tokens_a.extend(sample[j])
            tokens_b = []
            is_next_random = False
        
        # truncate to `target_sequence_length`.
        max_num_tokens = target_seq_length
        tokens_a, tokens_b = self.truncate_seq_pair(tokens_a, tokens_b, max_num_tokens, np_rng)

        tokens, token_types = self.create_tokens_and_token_types(tokens_a, tokens_b)

        tokens, masked_positions, masked_labels = self.create_masked_lm_predictions(tokens, np_rng)

        tokens_np, token_types_np, labels_np, padding_mask_np, loss_mask_np = self.pad_and_convert_to_numpy(tokens, token_types, masked_positions, masked_labels)

        train_sample = {
            'text': tokens_np,
            'types': token_types_np,
            'labels': labels_np,
            'is_random': int(is_next_random),
            'loss_mask': loss_mask_np,
            'padding_mask': padding_mask_np,
            'truncated': int(truncated)}
        }
        return train_sample

    def create_random_sentence_pair(self, sample, np_rng):
        """
        this is SOP (sentences order prediction), not NSP (next sentence prediction) task.
        According to ALBert, SOP is better than NSP for model to learn sentence's representation.
        """
        num_sentences = len(sample)
        assert num_sentences > 1, 'make sure each sample has at least two sentences.'

        a_end = 1
        if num_sentences >= 3:
            a_end = np_rng.randint(1, num_sentences)
        tokens_a = []
        for j in range(a_end):
            tokens_a.extend(sample[j])
        
        tokens_b = []
        for j in range(a_end, num_sentences):
            tokens_b.extend(sample[j])
        
        is_next_random = False
        if np_rng.random() < 0.5:
            is_next_random = True
            tokens_a, tokens_b = tokens_b, tokens_a
        
        return tokens_a, tokens_b, is_random_next
    
    def truncate_seq_pair(self, tokens_a, tokens_b, max_num_tokens, np_rng):
        """truncate sequence pair to a maximum sequence length"""
        len_a, len_b = len(tokens_a), len(tokens_b)
        while True:
            total_length = len_a + len_b
            if total_length <= max_num_tokens:
                break
            if len_a > len_b:
                trunc_tokens = tokens_a
                len_a -= 1
            else:
                trunc_tokens = tokens_b
                len_b -= 1
            
            if rng.random() < 0.5:
                trunc_tokens.pop(0) # remove the first element
            else:
                trunc_tokens.pop() # remove the last element
            
        return tokens_a, tokens_b
    
    def create_tokens_and_token_types(self, tokens_a, tokens_b):
        """merge segments A and B, add [CLS] and [SEP] and build token types."""
        tokens = [self.cls_id] + tokens_a + [self.sep_id] + tokens_b + [self.sep_id]
        token_types = [0] * (len(tokens_a) + 2) + [1] * (len(tokens_b) + 1)
        return tokens, token_types

    def mask_token(self, idx, tokens, np_rng):
        """
        helper function to mask `idx` token from `tokens` according to
        section 3.3.1 of https://arxiv.org/pdf/1810.04805.pdf
        """
        label = tokens[idx]
        if np_rng.random() < 0.8:
            new_label = self.mask_id
        else:
            if np_rng.random() < 0.5:
                new_label = label
            else:
                new_label = self.vocab_id_list[np.rng.randint(0, len(self.vocab_id_list))]
        
        tokens[idx] = new_label

        return label
   
    def create_masked_lm_predictions(self, tokens, np_rng, max_ngrams=3, do_whole_word_mask=True,
                                     favor_longer_ngram=False, geometric_dist=False):
        """Creates the predictions for the masked LM objective.
        Note: Tokens here are vocab ids and not text tokens."""

        masked_positions = []
        masked_labels = []

        if self.mask_lm_prob == 0:
            return output_tokens, masked_positions, masked_labels

        cand_indexes = []
        for (i, token) in enumerate(tokens):
            if token == self.cls_id or token == self.sep_id:
                continue
            # Whole Word Masking means that if we mask all of the wordpieces
            # corresponding to an original word.
            #
            # Note that Whole Word Masking does *not* change the training code
            # at all -- we still predict each WordPiece independently, softmaxed
            # over the entire vocabulary.
            if do_whole_word_mask and len(cand_indexes) >= 1 and not is_start_piece(self.vocab_id_to_token_dict[token]):
                cand_indexes[-1].append(i)
            else:
                cand_indexes.append([i])

        output_tokens = list(tokens)

        num_to_predict = min(self.max_preds_per_seq, max(1, int(round(len(tokens) * self.mask_lm_prob))))

        ngrams = np.arange(1, max_ngrams + 1, dtype=np.int64)
        if not geometric_dist:
            # By default, we set the probilities to favor shorter ngram sequences.
            pvals = 1. / np.arange(1, max_ngrams + 1)
            pvals /= pvals.sum(keepdims=True)
            if favor_longer_ngram:
                pvals = pvals[::-1]
        
        ngram_indexes = []
        for idx in range(len(cand_indexes)):
            ngram_index = []
            for n in ngrams:
                ngram_index.append(cand_indexes[idx:idx + n])
            ngram_indexes.append(ngram_index)
        
        np_rng.shuffle(ngram_indexes)

        masked_lms = []
        covered_indexes = set()        
        for cand_index_set in ngram_indexes:
            if len(masked_lms) >= num_to_predict:
                break
            if not cand_index_set:
                continue
            # Skip current piece if they are covered in lm masking or previous ngrams.
            for index_set in cand_index_set[0]:
                for index in index_set:
                    if index in covered_indexes:
                        continue

            if not geometric_dist:
                n = np_rng.choice(ngrams[:len(cand_index_set)],
                                p=pvals[:len(cand_index_set)] /
                                pvals[:len(cand_index_set)].sum(keepdims=True))
            else:
                # Sampling "n" from the geometric distribution and clipping it to
                # the max_ngrams. Using p=0.2 default from the SpanBERT paper
                # https://arxiv.org/pdf/1907.10529.pdf (Sec 3.1)
                n = min(np_rng.geometric(0.2), max_ngrams)

            index_set = sum(cand_index_set[n - 1], [])
            n -= 1
            # Repeatedly looking for a candidate that does not exceed the
            # maximum number of predictions by trying shorter ngrams.
            while len(masked_lms) + len(index_set) > num_to_predict:
                if n == 0:
                    break
                index_set = sum(cand_index_set[n - 1], [])
                n -= 1
            # If adding a whole-word mask would exceed the maximum number of
            # predictions, then just skip this candidate.
            if len(masked_lms) + len(index_set) > num_to_predict:
                continue
            is_any_index_covered = False
            for index in index_set:
                if index in covered_indexes:
                    is_any_index_covered = True
                    break
            if is_any_index_covered:
                continue
            for index in index_set:
                covered_indexes.add(index)
                label = self.mask_token(index, output_tokens, np_rng)
                masked_lms.append(MaskedLmInstance(index=index, label=label))
        
        masked_lms = sorted(masked_lms, key=lambda x: x.index)
        for p in masked_lms:
            masked_positions.append(p.index)
            masked_labels.append(p.label)

        return output_tokens, masked_positions, masked_labels
  
    def pad_and_convert_to_numpy(self, tokens, token_types, masked_positions, masked_labels):
        """pad sequences and convert them to numpy array"""

        # check
        num_tokens = len(tokens)
        num_pad = self.max_seq_length - num_tokens
        assert num_pad >= 0
        assert len(token_types) == num_tokens
        assert len(masked_positions) == len(masked_labels)

        # tokens and token types
        filler = [self.pad_id] * num_pad
        tokens_np = np.array(tokens + filler, dtype=np.int64)
        token_types_np = np.array(token_types + filler, dtype=np.int64)

        # padding mask
        padding_mask_np = np.array([1] * num_tokens + [0] * num_pad, dtype=np.int64)

        # labels and loss mask
        labels = [-1] * self.max_seq_length
        loss_mask = [0] * self.max_seq_length
        for idx, label in zip(masked_positions, masked_labels):
            assert idx < num_tokens
            labels[idx] = label
            loss_mask[idx] = 1
        labels_np = np.array(labels, dtype=np.int64)
        loss_mask_np = np.array(loss_mask, dtype=np.int64)

        return tokens_np, token_types_np, labels_np, padding_mask_np, loss_mask_np

