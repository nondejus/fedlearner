# Copyright 2020 The FedLearner Authors. All Rights Reserved.
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

# coding: utf-8

import os
import logging
import uuid
import threading
import time
from contextlib import contextmanager
from collections import OrderedDict

from guppy import hpy

import tensorflow_io # pylint: disable=unused-import
import tensorflow.compat.v1 as tf
from google.protobuf import text_format
from tensorflow.compat.v1 import gfile

from fedlearner.common import common_pb2 as common_pb
from fedlearner.common import data_join_service_pb2 as dj_pb

DataBlockSuffix = '.data'
DataBlockMetaSuffix = '.meta'
RawDataMetaPrefix = 'raw_data_'
RawDataPubSuffix = '.pub'
InvalidExampleId = ''.encode()
TmpFileSuffix = '.tmp'
DoneFileSuffix = '.done'
RawDataFileSuffix = '.rd'
InvalidEventTime = -9223372036854775808
InvalidRawId = ''.encode()

@contextmanager
def make_tf_record_iter(fpath, options=None):
    record_iter = None
    expt = None
    try:
        record_iter = tf.io.tf_record_iterator(fpath, options)
        yield record_iter
    except Exception as e: # pylint: disable=broad-except
        logging.warning("Failed make tf_record_iterator for "\
                        "%s, reason %s", fpath, e)
        expt = e
    if record_iter is not None:
        del record_iter
    if expt is not None:
        raise expt

def partition_repr(partition_id):
    return 'partition_{:04}'.format(partition_id)

def encode_data_block_meta_fname(data_source_name,
                                 partition_id,
                                 data_block_index):
    return '{}.{}.{:08}{}'.format(
            data_source_name, partition_repr(partition_id),
            data_block_index, DataBlockMetaSuffix
        )

def encode_block_id(data_source_name, meta):
    return '{}.{}.{:08}.{}-{}'.format(
            data_source_name, partition_repr(meta.partition_id),
            meta.data_block_index, meta.start_time, meta.end_time
        )

def decode_block_id(block_id):
    segs = block_id.split('.')
    if len(segs) != 4:
        raise ValueError("{} invalid. Segmenet of block_id split "\
                          "by . shoud be 4".format(block_id))
    data_source_name = segs[0]
    partition_id = int(segs[1][len('partition_'):])
    data_block_index = int(segs[2])
    time_frame_segs = segs[3].split('-')
    if len(time_frame_segs) != 2:
        raise ValueError("{} invalid. Segmenet of time frame split "
                         "by - should be 2".format(block_id))
    start_time, end_time = int(time_frame_segs[0]), int(time_frame_segs[1])
    return {"data_source_name": data_source_name,
            "partition_id": partition_id,
            "data_block_index": data_block_index,
            "time_frame": (start_time, end_time)}

def encode_data_block_fname(data_source_name, meta):
    block_id = encode_block_id(data_source_name, meta)
    return '{}{}'.format(block_id, DataBlockSuffix)

def load_data_block_meta(meta_fpath):
    assert meta_fpath.endswith(DataBlockMetaSuffix)
    if not gfile.Exists(meta_fpath):
        return None
    with make_tf_record_iter(meta_fpath) as fitr:
        return text_format.Parse(next(fitr).decode(), dj_pb.DataBlockMeta())

def data_source_etcd_base_dir(data_source_name):
    return os.path.join('data_source', data_source_name)

def retrieve_data_source(etcd, data_source_name):
    etcd_key = data_source_etcd_base_dir(data_source_name)
    raw_data = etcd.get_data(etcd_key)
    if raw_data is None:
        raise ValueError("etcd master key is None for {}".format(
            data_source_name)
        )
    return text_format.Parse(raw_data, common_pb.DataSource())

def commit_data_source(etcd, data_source):
    etcd_key = data_source_etcd_base_dir(data_source.data_source_meta.name)
    etcd.set_data(etcd_key, text_format.MessageToString(data_source))

def partition_manifest_etcd_key(data_source_name, partition_id):
    return os.path.join(data_source_etcd_base_dir(data_source_name),
                        'raw_data_dir', partition_repr(partition_id))

def raw_data_meta_etcd_key(data_source_name, partition_id, process_index):
    manifest_etcd_key = partition_manifest_etcd_key(data_source_name,
                                                    partition_id)
    return os.path.join(manifest_etcd_key,
                        '{}{:08}'.format(RawDataMetaPrefix, process_index))

def example_id_anchor_etcd_key(data_source_name, partition_id):
    etcd_base_dir = data_source_etcd_base_dir(data_source_name)
    return os.path.join(etcd_base_dir, 'dumped_example_id_anchor',
                        partition_repr(partition_id))

def raw_data_pub_etcd_key(pub_base_dir, partition_id, process_index):
    return os.path.join(pub_base_dir, partition_repr(partition_id),
                        '{:08}{}'.format(process_index, RawDataPubSuffix))

_valid_basic_feature_type = (int, str, float)
def convert_dict_to_tf_example(src_dict):
    assert isinstance(src_dict, dict)
    tf_feature = {}
    for key, feature in src_dict.items():
        if not isinstance(key, str):
            raise RuntimeError('the key {}({}) of dict must a '\
                               'string'.format(key, type(key)))
        basic_type = type(feature)
        if basic_type == str and key not in ('example_id', 'raw_id'):
            if feature.lstrip('-').isdigit():
                feature = int(feature)
                basic_type = int
            else:
                try:
                    feature = float(feature)
                    basic_type = float
                except ValueError as e:
                    pass
        if isinstance(type(feature), list):
            if len(feature) == 0:
                logging.debug('skip %s since feature is empty list', key)
                continue
            basic_type = feature[0]
            if not all(isinstance(x, basic_type) for x in feature):
                raise RuntimeError('type of elements in feature of key {} '\
                                   'is not the same'.format(key))
        if not isinstance(feature, _valid_basic_feature_type):
            raise RuntimeError("feature type({}) of key {} is not support "\
                               "for tf Example".format(basic_type, key))
        if basic_type == int:
            value = feature if isinstance(feature, list) else [feature]
            tf_feature[key] = tf.train.Feature(
                int64_list=tf.train.Int64List(value=value))
        elif basic_type == str:
            value = [feat.encode() for feat in feature] if \
                     isinstance(feature, list) else [feature.encode()]
            tf_feature[key] = tf.train.Feature(
                bytes_list=tf.train.BytesList(value=value))
        else:
            assert basic_type == float
            value = feature if isinstance(feature, list) else [feature]
            tf_feature[key] = tf.train.Feature(
                float_list=tf.train.FloatList(value=value))
    return tf.train.Example(features=tf.train.Features(feature=tf_feature))

def convert_tf_example_to_dict(src_tf_example):
    assert isinstance(src_tf_example, tf.train.Example)
    dst_dict = OrderedDict()
    tf_feature = src_tf_example.features.feature
    for key, feat in tf_feature.items():
        csv_val = None
        if feat.HasField('int64_list'):
            csv_val = [item for item in feat.int64_list.value] # pylint: disable=unnecessary-comprehension
        elif feat.HasField('bytes_list'):
            csv_val = [item.decode() for item in feat.bytes_list.value] # pylint: disable=unnecessary-comprehension
        elif feat.HasField('float_list'):
            csv_val = [item for item in feat.float_list.value] #pylint: disable=unnecessary-comprehension
        else:
            assert False, "feat type must in int64, byte, float"
        assert isinstance(csv_val, list)
        dst_dict[key] = csv_val[0] if len(csv_val) == 1 else csv_val
    return dst_dict

def int2bytes(digit, byte_len, byteorder='little'):
    return int(digit).to_bytes(byte_len, byteorder)

def bytes2int(byte, byteorder='little'):
    return int.from_bytes(byte, byteorder)

def gen_tmp_fpath(fdir):
    return os.path.join(fdir, str(uuid.uuid1())+TmpFileSuffix)

def portal_etcd_base_dir(portal_name):
    return os.path.join('portal', portal_name)

def portal_job_etcd_key(portal_name, job_id):
    return os.path.join(portal_etcd_base_dir(portal_name), 'job_dir',
                        '{:08}.pj'.format(job_id))

def portal_job_part_etcd_key(portal_name, job_id, partition_id):
    return os.path.join(portal_job_etcd_key(portal_name, job_id),
                        partition_repr(partition_id))

def portal_map_output_dir(map_base_dir, job_id):
    return os.path.join(map_base_dir, 'map_{:08}'.format(job_id))

def portal_reduce_output_dir(reduce_base_dir, job_id):
    return os.path.join(reduce_base_dir, 'reduce_{:08}'.format(job_id))

def data_source_data_block_dir(data_source):
    return os.path.join(data_source.output_base_dir, 'data_block')

def data_source_example_dumped_dir(data_source):
    return os.path.join(data_source.output_base_dir, 'example_dump')

class _OomRsikChecker(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._mem_limit = int(os.environ.get('MEM_LIMIT', '17179869184'))
        self._latest_updated_ts = 0
        self._heap_memory_usage = None
        self._try_update_memory_usage(True)


    def _try_update_memory_usage(self, force):
        if time.time() - self._latest_updated_ts >= 0.7 or force:
            self._heap_memory_usage = hpy().heap().size
            self._latest_updated_ts = time.time()

    def check_oom_risk(self, water_level_percent=0.9, force=False):
        with self._lock:
            self._try_update_memory_usage(force)
            reserved_mem = int(self._mem_limit * 0.5)
            if reserved_mem >= 2 << 30:
                reserved_mem = 2 << 30
            avail_mem = self._mem_limit - reserved_mem
            return self._heap_memory_usage >= avail_mem * water_level_percent

_oom_risk_checker = _OomRsikChecker()
def get_oom_risk_checker():
    return _oom_risk_checker
