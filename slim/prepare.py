#-*- coding: utf-8 -*-
#from __future__ import unicode_literals
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import os
import random
import sys
import threading

import numpy as np
import tensorflow as tf

from datasets.dataset_utils import write_label_file, int64_feature, bytes_feature


# Initialize Global Variables
tf.app.flags.DEFINE_string('project_name', 'uspace', 'Project Name')
tf.app.flags.DEFINE_string('data_directory', '', 'Data directory')
tf.app.flags.DEFINE_string('output_directory', '', 'Output data directory')
tf.app.flags.DEFINE_integer('num_threads', 4, 'Number of threads to preprocess the images.')
tf.app.flags.DEFINE_string('labels_file', '', 'Labels file')
tf.app.flags.DEFINE_string('K', 5, '(train+validation) / validation')

FLAGS = tf.app.flags.FLAGS


def _convert_to_example(filename, image_buffer, label, text, height, width):
    colorspace = 'RGB'
    channels = 3
    image_format = 'JPEG'

    example = tf.train.Example(features=tf.train.Features(feature={
        'image/height': int64_feature(height),
        'image/width': int64_feature(width),
        'image/colorspace': bytes_feature(colorspace),
        'image/channels': int64_feature(channels),
        'image/class/label': int64_feature(label),
        'image/class/text': bytes_feature(text),
        'image/format': bytes_feature(image_format),
        'image/filename': bytes_feature(os.path.basename(filename)),
        'image/encoded': bytes_feature(image_buffer)}))
    return example


class ImageCoder(object):
    def __init__(self):
        self._sess = tf.Session()
        self._png_data = tf.placeholder(dtype=tf.string)
        image = tf.image.decode_png(self._png_data, channels=3)
        self._png_to_jpeg = tf.image.encode_jpeg(image, format='rgb', quality=100)
        self._decode_jpeg_data = tf.placeholder(dtype=tf.string)
        self._decode_jpeg = tf.image.decode_jpeg(self._decode_jpeg_data, channels=3)

    def png_to_jpeg(self, image_data):
        return self._sess.run(self._png_to_jpeg, feed_dict={self._png_data: image_data})

    def decode_jpeg(self, image_data):
        image = self._sess.run(self._decode_jpeg, feed_dict={self._decode_jpeg_data: image_data})
        assert len(image.shape) == 3
        assert image.shape[2] == 3
        return image


def _is_png(filename):
    return '.png' in filename


def _process_image(filename, coder):
    image_data = tf.gfile.FastGFile(filename, 'r').read()
    if _is_png(filename):
        image_data = coder.png_to_jpeg(image_data)
    image = coder.decode_jpeg(image_data)

    # Check that image converted to RGB
    assert len(image.shape) == 3
    height = image.shape[0]
    width = image.shape[1]
    assert image.shape[2] == 3

    return image_data, height, width


def _process_image_files_batch(coder, thread_index, ranges, name, filenames, texts, labels, num_shards):
    num_threads = len(ranges)
    assert not num_shards % num_threads
    num_shards_per_batch = int(num_shards / num_threads)

    shard_ranges = np.linspace(ranges[thread_index][0],
                             ranges[thread_index][1],
                             num_shards_per_batch + 1).astype(int)
    num_files_in_thread = ranges[thread_index][1] - ranges[thread_index][0]

    counter = 0
    for s in xrange(num_shards_per_batch):
        shard = thread_index * num_shards_per_batch + s
        output_filename = '%s-%s-%.3d-of-%.3d.tfrecord' % (FLAGS.project_name, name, shard, num_shards)
        output_file = os.path.join(FLAGS.output_directory, output_filename)
        writer = tf.python_io.TFRecordWriter(output_file)

        shard_counter = 0
        files_in_shard = np.arange(shard_ranges[s], shard_ranges[s + 1], dtype=int)
        for i in files_in_shard:
            filename = filenames[i]
            label = labels[i]
            text = texts[i]

            image_buffer, height, width = _process_image(filename, coder)

            example = _convert_to_example(filename, image_buffer, label,
                                        text, height, width)
            writer.write(example.SerializeToString())
            shard_counter += 1
            counter += 1

            if not counter % 1000:
                print('%s [thread %d]: Processed %d of %d images in thread batch.' %
                      (datetime.now(), thread_index, counter, num_files_in_thread))
                sys.stdout.flush()

        print('%s [thread %d]: Wrote %d images to %s' %
              (datetime.now(), thread_index, shard_counter, output_file))
        sys.stdout.flush()
        shard_counter = 0
    print('%s [thread %d]: Wrote %d images to %d shards.' %
        (datetime.now(), thread_index, counter, num_files_in_thread))
    sys.stdout.flush()


def _process_image_files(name, filenames, texts, labels, num_shards):
    assert len(filenames) == len(texts)
    assert len(filenames) == len(labels)

    spacing = np.linspace(0, len(filenames), FLAGS.num_threads + 1).astype(np.int)
    ranges = []
    threads = []
    for i in xrange(len(spacing) - 1):
        ranges.append([spacing[i], spacing[i+1]])

    # Launch a thread for each batch.
    print('Launching %d threads for spacings: %s' % (FLAGS.num_threads, ranges))
    sys.stdout.flush()

    # Create a mechanism for monitoring when all threads are finished.
    coord = tf.train.Coordinator()

    # Create a generic TensorFlow-based utility for converting all image codings.
    coder = ImageCoder()

    threads = []
    for thread_index in xrange(len(ranges)):
        args = (coder, thread_index, ranges, name, filenames,
                texts, labels, num_shards)
        t = threading.Thread(target=_process_image_files_batch, args=args)
        t.start()
        threads.append(t)

    # Wait for all the threads to terminate.
    coord.join(threads)
    print('%s: Finished writing all %d images in data set.' %
        (datetime.now(), len(filenames)))
    sys.stdout.flush()


def _find_image_files(data_dir, labels_file):
    print('Determining list of input files and labels from %s.' % data_dir)
    unique_labels = [l.strip() for l in tf.gfile.FastGFile(labels_file, 'r').readlines() if l.strip()]

    labels = []
    filenames = []
    texts = []
    labels_to_texts = dict()

    # Leave label index 0 empty as a background class.
    label_index = 1

    # Find minimun class count
    min_class_cnt = None
    for text in unique_labels:
        jpeg_file_path = os.path.join(data_dir, text, '*')
        matching_files = tf.gfile.Glob(jpeg_file_path)
        if not min_class_cnt or min_class_cnt >= len(matching_files):
            min_class_cnt = len(matching_files)

    # Construct the list of JPEG files and labels.
    for text in unique_labels:
        labels_to_texts[label_index] = text
        jpeg_file_path = os.path.join(data_dir, text, '*')
        matching_files = tf.gfile.Glob(jpeg_file_path)
        matching_files = random.sample(matching_files, min_class_cnt)

        labels.extend([label_index] * len(matching_files))
        texts.extend([text] * len(matching_files))
        filenames.extend(matching_files)

        if not label_index % 100:
          print('Finished finding files in %d of %d classes.' % (
              label_index, len(labels)))
        label_index += 1

    shuffled_index = range(len(filenames))
    random.shuffle(shuffled_index)

    filenames = [filenames[i] for i in shuffled_index]
    texts = [texts[i] for i in shuffled_index]
    labels = [labels[i] for i in shuffled_index]

    print('Found %d JPEG files across %d labels inside %s.' % (len(filenames), len(unique_labels), data_dir))
    return filenames, texts, labels, labels_to_texts


def _process_dataset(directory, labels_file):
    filenames, texts, labels, labels_to_texts = _find_image_files(directory, labels_file)
    write_label_file(labels_to_texts, FLAGS.output_directory)
    r = np.linspace(0, len(filenames), FLAGS.K+1).astype(int)
    _process_image_files('validation', filenames[:r[1]], texts[:r[1]], labels[:r[1]], FLAGS.num_threads)
    _process_image_files('train', filenames[r[1]:], texts[r[1]:], labels[r[1]:], FLAGS.num_threads*(FLAGS.K-1))

    print('')
    print('total count = %d' % len(filenames))
    print('validation count = %d' % len(filenames[:r[1]]))
    print('train count = %d' % len(filenames[r[1]:]))


def main(_):
    if not FLAGS.data_directory:
        FLAGS.data_directory = '/home/gulby/tmp/%s/data' % FLAGS.project_name
    if not FLAGS.output_directory:
        FLAGS.output_directory = '/home/gulby/tmp/%s/output' % FLAGS.project_name
    if not FLAGS.labels_file:
        FLAGS.labels_file = '%s/labels.txt' % FLAGS.data_directory

    print('Saving results to %s' % FLAGS.output_directory)

    # Run it!
    _process_dataset(FLAGS.data_directory, FLAGS.labels_file)


if __name__ == '__main__':
    tf.app.run()
