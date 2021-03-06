# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

import argparse

import numpy as np
import paddle
import paddlenlp as ppnlp
from scipy.special import softmax
from paddle import inference
from paddlenlp.data import Stack, Tuple, Pad

# yapf: disable
parser = argparse.ArgumentParser()
parser.add_argument("--model_file", type=str, required=True, default='./static_graph_params.pdmodel', help="The path to model info in static graph.")
parser.add_argument("--params_file", type=str, required=True, default='./static_graph_params.pdiparams', help="The path to parameters in static graph.")

parser.add_argument("--max_seq_length", default=128, type=int, help="The maximum total input sequence length after tokenization. "
    "Sequences longer than this will be truncated, sequences shorter will be padded.")
parser.add_argument("--batch_size", default=2, type=int, help="Batch size per GPU/CPU for training.")
parser.add_argument('--device', choices=['cpu', 'gpu', 'xpu'], default="gpu", help="Select which device to train model, defaults to gpu.")
args = parser.parse_args()
# yapf: enable


def convert_example(example,
                    tokenizer,
                    label_list,
                    max_seq_length=512,
                    is_test=False):
    """
    Builds model inputs from a sequence or a pair of sequence for sequence classification tasks
    by concatenating and adding special tokens. And creates a mask from the two sequences passed 
    to be used in a sequence-pair classification task.
        
    A BERT sequence has the following format:

    - single sequence: ``[CLS] X [SEP]``
    - pair of sequences: ``[CLS] A [SEP] B [SEP]``

    A BERT sequence pair mask has the following format:
    ::
        0 0 0 0 0 0 0 0 0 0 0 1 1 1 1 1 1 1 1 1
        | first sequence    | second sequence |

    If only one sequence, only returns the first portion of the mask (0's).


    Args:
        example(obj:`list[str]`): List of input data, containing text and label if it have label.
        tokenizer(obj:`PretrainedTokenizer`): This tokenizer inherits from :class:`~paddlenlp.transformers.PretrainedTokenizer` 
            which contains most of the methods. Users should refer to the superclass for more information regarding methods.
        label_list(obj:`list[str]`): All the labels that the data has.
        max_seq_len(obj:`int`): The maximum total input sequence length after tokenization. 
            Sequences longer than this will be truncated, sequences shorter will be padded.
        is_test(obj:`False`, defaults to `False`): Whether the example contains label or not.

    Returns:
        input_ids(obj:`list[int]`): The list of token ids.
        segment_ids(obj: `list[int]`): List of sequence pair mask.
        label(obj:`numpy.array`, data type of int64, optional): The input label if not is_test.
    """
    text = example
    encoded_inputs = tokenizer(text=text, max_seq_len=max_seq_length)
    input_ids = encoded_inputs["input_ids"]
    segment_ids = encoded_inputs["token_type_ids"]

    if not is_test:
        # create label maps
        label_map = {}
        for (i, l) in enumerate(label_list):
            label_map[l] = i

        label = label_map[label]
        label = np.array([label], dtype="int64")
        return input_ids, segment_ids, label
    else:
        return input_ids, segment_ids


class Predictor(object):
    def __init__(self, model_file, params_file, device, max_seq_length):
        self.max_seq_length = max_seq_length

        config = paddle.inference.Config(model_file, params_file)
        if device == "gpu":
            # set GPU configs accordingly
            config.enable_use_gpu(100, 0)
        elif device == "cpu":
            # set CPU configs accordingly,
            # such as enable_mkldnn, set_cpu_math_library_num_threads
            config.disable_gpu()
        elif device == "xpu":
            # set XPU configs accordingly
            config.enable_xpu(100)
        config.switch_use_feed_fetch_ops(False)
        self.predictor = paddle.inference.create_predictor(config)

        self.input_handles = [
            self.predictor.get_input_handle(name)
            for name in self.predictor.get_input_names()
        ]

        self.output_handle = self.predictor.get_output_handle(
            self.predictor.get_output_names()[0])

    def predict(self, data, tokenizer, label_map, batch_size=1):
        """
        Predicts the data labels.

        Args:
            model (obj:`paddle.nn.Layer`): A model to classify texts.
            data (obj:`List(Example)`): The processed data whose each element is a Example (numedtuple) object.
                A Example object contains `text`(word_ids) and `se_len`(sequence length).
            tokenizer(obj:`PretrainedTokenizer`): This tokenizer inherits from :class:`~paddlenlp.transformers.PretrainedTokenizer` 
                which contains most of the methods. Users should refer to the superclass for more information regarding methods.
            label_map(obj:`dict`): The label id (key) to label str (value) map.
            batch_size(obj:`int`, defaults to 1): The number of batch.

        Returns:
            results(obj:`dict`): All the predictions labels.
        """
        examples = []
        for text in data:
            input_ids, segment_ids = convert_example(
                text,
                tokenizer,
                label_list=label_map.values(),
                max_seq_length=self.max_seq_length,
                is_test=True)
            examples.append((input_ids, segment_ids))

        batchify_fn = lambda samples, fn=Tuple(
            Pad(axis=0, pad_val=tokenizer.pad_token_id),  # input
            Pad(axis=0, pad_val=tokenizer.pad_token_id),  # segment
        ): fn(samples)

        # Seperates data into some batches.
        batches = [
            examples[idx:idx + batch_size]
            for idx in range(0, len(examples), batch_size)
        ]

        results = []
        for batch in batches:
            input_ids, segment_ids = batchify_fn(batch)
            self.input_handles[0].copy_from_cpu(input_ids)
            self.input_handles[1].copy_from_cpu(segment_ids)
            self.predictor.run()
            logits = self.output_handle.copy_to_cpu()
            probs = softmax(logits, axis=1)
            idx = np.argmax(probs, axis=1)
            idx = idx.tolist()
            labels = [label_map[i] for i in idx]
            results.extend(labels)
        return results


if __name__ == "__main__":
    # Define predictor to do prediction.
    predictor = Predictor(args.model_file, args.params_file, args.device,
                          args.max_seq_length)

    # ErnieTinyTokenizer is special for ernie-tiny pretained model.
    tokenizer = ppnlp.transformers.ErnieTinyTokenizer.from_pretrained(
        'ernie-tiny')

    data = [
        '??????????????????????????????????????????????????????????????????????????????',
        '?????????????????????????????????????????????????????????????????????????????????????????????????????????????????????',
        '????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????????',
    ]
    label_map = {0: 'negative', 1: 'positive'}

    results = predictor.predict(
        data, tokenizer, label_map, batch_size=args.batch_size)
    for idx, text in enumerate(data):
        print('Data: {} \t Label: {}'.format(text, results[idx]))
