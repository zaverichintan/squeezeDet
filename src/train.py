# Author: Bichen Wu (bichen@berkeley.edu) 08/25/2016

"""Train"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import cv2
from datetime import datetime
import os.path
import sys
import time
import copy

import numpy as np
from six.moves import xrange
import tensorflow as tf
import threading

from config import *
from dataset import pascal_voc, kitti, cityscape
from utils.util import sparse_to_dense, bgr_to_rgb, bbox_transform2, bbox_transform_inv2, bbox_transform
from nets import *
from dataset.input_reader import decode_parameterization

FLAGS = tf.app.flags.FLAGS

tf.app.flags.DEFINE_string('dataset', 'KITTI',
                           """Currently only support KITTI and CITYSCAPE datasets.""")
tf.app.flags.DEFINE_string('data_path', '', """Root directory of data""")
tf.app.flags.DEFINE_string('image_set', 'train',
                           """ Can be train, trainval, val, or test""")
tf.app.flags.DEFINE_string('year', '2007',
                            """VOC challenge year. 2007 or 2012"""
                            """Only used for Pascal VOC dataset""")
tf.app.flags.DEFINE_string('train_dir', '/tmp/bichen/logs/squeezeDet/train',
                            """Directory where to write event logs """
                            """and checkpoint.""")
tf.app.flags.DEFINE_integer('max_steps', 200000,
                            """Maximum number of batches to run.""")
tf.app.flags.DEFINE_string('net', 'squeezeDet',
                           """Neural net architecture. """)
tf.app.flags.DEFINE_string('pretrained_model_path', '',
                           """Path to the pretrained model.""")
tf.app.flags.DEFINE_integer('summary_step', 10,
                            """Number of steps to save summary.""")
tf.app.flags.DEFINE_integer('checkpoint_step', 1000,
                            """Number of steps to save checkpoint.""")
tf.app.flags.DEFINE_string('gpu', '0', """gpu id.""")
tf.app.flags.DEFINE_integer('mask_parameterization', 4,
                            """Bounding box is 4, octagonal mask is 8. other values not supported""")
tf.app.flags.DEFINE_boolean('eval_valid', False, """Evaluate on validation set every summary step ?""")
tf.app.flags.DEFINE_boolean('log_anchors', False, """Use Log domain extracted anchors ?""")
tf.app.flags.DEFINE_boolean('bounding_box_checkpoint', False, """Is the checkpoint file for bounding box prediction ?""")
tf.app.flags.DEFINE_boolean('only_tune_last_layer', False, """Show only the last layer be trained ?""")
tf.app.flags.DEFINE_float('warm_restart_lr', -1.0,
                            """Learning rate to be used after warm restart""")
tf.app.flags.DEFINE_string('encoding_type', 'normal',
                            """what type of encoding to use""")

def _draw_box(im, box_list_pre, label_list, color=None, cdict=None, form='center', draw_masks=False, fill=False, fps_text='NA'):
  assert form == 'center' or form == 'diagonal', \
      'bounding box format not accepted: {}.'.format(form)
  bkp_im = copy.deepcopy(im)
  box_list = copy.deepcopy(box_list_pre)
  ht, wd, ch = np.shape(im)
  for bbox, label in zip(box_list, label_list):
    if form == 'center':
      if draw_masks:
        raw_bounding_box = bbox
        bbox = bbox_transform2(bbox)
      else:
        bbox[0:4] = bbox_transform(bbox[0:4])
    else:
      if draw_masks:
        raw_bounding_box = bbox_transform_inv2(bbox)

    xmin, ymin, xmax, ymax = [int(bbox[o]) for o in range(len(bbox)) if o < 4]
    if draw_masks:
      points = decode_parameterization(raw_bounding_box)
      points = np.round(points) # Ensure rounding
      points = np.array(points, 'int32')

    l = label.split(':')[0] # text before "CLASS: (PROB)"
    if cdict and l in cdict:
      c = cdict[l] # if color dict is provided , use it
    else:
      if color == None: # if color is provided use it or use random colors
        c = (np.random.choice(256), np.random.choice(256), np.random.choice(256))
      else:
        c = color
    # FPS counter
    if fps_text != 'NA':
      fps_counter, per_frame_time = fps_text.split('/')
      textSize, base = cv2.getTextSize(fps_counter, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1) # Get font size
      cv2.rectangle(im, (10, 10), (1014, 40), (0, 0, 0), cv2.FILLED)
      cv2.putText(im, fps_counter, (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
      cv2.putText(im, per_frame_time, (750, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    labelSize, baseLine = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.4, 1)
    label_ymin = max(ymin, labelSize[1] + 10)
    cv2.rectangle(im, (xmin, label_ymin-labelSize[1]-10), (xmin+labelSize[0], label_ymin+baseLine-10), c, cv2.FILLED)
    if not draw_masks:
      cv2.rectangle(im, (xmin, ymin), (xmax, ymax), c, 2)
    # draw label
    y_lim = max(ymin-3, 0)
    font = cv2.FONT_HERSHEY_DUPLEX
    if draw_masks:
      if fill:
        color_mask = np.zeros((ht, wd, 3), np.uint8)
        cv2.fillConvexPoly(color_mask, points, c)
        im[color_mask > 0] = bkp_im[color_mask > 0]
        im[color_mask > 0] = 0.5*im[color_mask > 0]  + 0.5*color_mask[color_mask > 0]
      cv2.putText(im, label, (xmin, label_ymin-7), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1) # Draw label text
      for p in range(len(points)):
        cv2.line(im, tuple(points[p]), tuple(points[(p+1)%len(points)]), c, 2)
    else:
      cv2.putText(im, label, (xmin, label_ymin-7), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1) # Draw label text

def _viz_prediction_result(model, images, bboxes, labels, batch_det_bbox,
                           batch_det_class, batch_det_prob, visualize_gt_masks=False, visualize_pred_masks=False, edge_adhesions=[]):
  mc = model.mc

  for i in range(len(images)):
    # draw ground truth
    if len(edge_adhesions) != 0:
      if len(edge_adhesions[0][0]) > 4:
        print_text =  [str(int(edge[0]))+str(int(edge[1]))+str(int(edge[2]))+str(int(edge[3]))+str(int(edge[4]))+str(int(edge[5]))+str(int(edge[6]))+str(int(edge[7])) for idx, edge in zip(labels[i], edge_adhesions[i])]
      else:
        print_text =  [str(int(edge[0]))+str(int(edge[1]))+str(int(edge[2]))+str(int(edge[3])) for idx, edge in zip(labels[i], edge_adhesions[i])]
      _draw_box(
          images[i], bboxes[i],
          #[mc.CLASS_NAMES[idx]+":"+str(int(edge[0]))+str(int(edge[1]))+str(int(edge[2]))+str(int(edge[3])) for idx, edge in zip(labels[i], edge_adhesions[i])],
          print_text, draw_masks=visualize_gt_masks, fill=True)
    else:
      _draw_box(
          images[i], bboxes[i],
          [mc.CLASS_NAMES[idx] for idx in labels[i]], (0, 255, 0),
          draw_masks=visualize_gt_masks, fill=False)

    # draw prediction
    det_bbox, det_prob, det_class = model.filter_prediction(
        batch_det_bbox[i], batch_det_prob[i], batch_det_class[i])

    keep_idx    = [idx for idx in range(len(det_prob)) \
                      if det_prob[idx] > mc.PLOT_PROB_THRESH]
    # print("Keep ids", keep_idx, max(det_prob))
    det_bbox    = [det_bbox[idx] for idx in keep_idx]
    det_prob    = [det_prob[idx] for idx in keep_idx]
    det_class   = [det_class[idx] for idx in keep_idx]

    _draw_box(
        images[i], det_bbox,
        [mc.CLASS_NAMES[idx]+': (%.2f)'% prob \
            for idx, prob in zip(det_class, det_prob)],
        (0, 0, 255), draw_masks=visualize_pred_masks, fill=False)


def train():
  """Train SqueezeDet model"""
  assert FLAGS.dataset == 'KITTI' or FLAGS.dataset == 'CITYSCAPE', \
      'Currently only support KITTI and CITYSCAPE datasets'
  assert FLAGS.mask_parameterization in [4,8], 'Values other than 4 and 8 are not supported !'

  os.environ['CUDA_VISIBLE_DEVICES'] = FLAGS.gpu

  with tf.Graph().as_default():

    assert FLAGS.net == 'vgg16' or FLAGS.net == 'resnet50' \
        or FLAGS.net == 'squeezeDet' or FLAGS.net == 'squeezeDet+', \
        'Selected neural net architecture not supported: {}'.format(FLAGS.net)
    if FLAGS.net == 'vgg16':
      if FLAGS.dataset == 'KITTI':
        mc = kitti_vgg16_config(FLAGS.mask_parameterization, FLAGS.only_tune_last_layer, FLAGS.encoding_type)
      elif FLAGS.dataset == 'CITYSCAPE':
        mc = cityscape_vgg16_config(FLAGS.mask_parameterization, FLAGS.log_anchors, FLAGS.only_tune_last_layer, FLAGS.encoding_type)
      mc.IS_TRAINING = True
      # mc.PRETRAINED_MODEL_PATH = FLAGS.pretrained_model_path
      print("Not using pretrained model for VGG, uncomment above line and comment below line to use pretrained model !")
      mc.LOAD_PRETRAINED_MODEL = False
      if FLAGS.warm_restart_lr != -1.0:
        print("Updating the learning rate for warm restart to", FLAGS.warm_restart_lr)
        mc.LEARNING_RATE = FLAGS.warm_restart_lr
      model = VGG16ConvDet(mc)
    elif FLAGS.net == 'resnet50':
      if FLAGS.dataset == 'KITTI':
        mc = kitti_res50_config(FLAGS.mask_parameterization, FLAGS.only_tune_last_layer, FLAGS.encoding_type)
      elif FLAGS.dataset == 'CITYSCAPE':
        mc = cityscape_res50_config(FLAGS.mask_parameterization, FLAGS.log_anchors, FLAGS.only_tune_last_layer, FLAGS.encoding_type)
      mc.IS_TRAINING = True
      mc.PRETRAINED_MODEL_PATH = FLAGS.pretrained_model_path
      if FLAGS.warm_restart_lr != -1.0:
        print("Updating the learning rate for warm restart to", FLAGS.warm_restart_lr)
        mc.LEARNING_RATE = FLAGS.warm_restart_lr
      model = ResNet50ConvDet(mc)
    elif FLAGS.net == 'squeezeDet':
      if FLAGS.dataset == 'KITTI':
        mc = kitti_squeezeDet_config(FLAGS.mask_parameterization, FLAGS.only_tune_last_layer, FLAGS.encoding_type)
      elif FLAGS.dataset == 'CITYSCAPE':
        mc = cityscape_squeezeDet_config(FLAGS.mask_parameterization, FLAGS.log_anchors, FLAGS.only_tune_last_layer, FLAGS.encoding_type)
      mc.IS_TRAINING = True
      mc.PRETRAINED_MODEL_PATH = FLAGS.pretrained_model_path
      if FLAGS.warm_restart_lr != -1.0:
        print("Updating the learning rate for warm restart to", FLAGS.warm_restart_lr)
        mc.LEARNING_RATE = FLAGS.warm_restart_lr
      model = SqueezeDet(mc)
    elif FLAGS.net == 'squeezeDet+':
      if FLAGS.dataset == 'KITTI':
        mc = kitti_squeezeDetPlus_config(FLAGS.mask_parameterization, FLAGS.only_tune_last_layer, FLAGS.encoding_type)
      elif FLAGS.dataset == 'CITYSCAPE':
        mc = cityscape_squeezeDetPlus_config(FLAGS.mask_parameterization, FLAGS.log_anchors, FLAGS.only_tune_last_layer, FLAGS.encoding_type)
      mc.IS_TRAINING = True
      mc.PRETRAINED_MODEL_PATH = FLAGS.pretrained_model_path
      if FLAGS.warm_restart_lr != -1.0:
        print("Updating the learning rate for warm restart to", FLAGS.warm_restart_lr)
        mc.LEARNING_RATE = FLAGS.warm_restart_lr
      model = SqueezeDetPlus(mc)

    imdb_valid = None
    if FLAGS.dataset == 'KITTI':
      imdb = kitti(FLAGS.image_set, FLAGS.data_path, mc)
      if FLAGS.eval_valid:
        imdb_valid = kitti('val', FLAGS.data_path, mc)
        imdb_valid.mc.DATA_AUGMENTATION = False
    elif FLAGS.dataset == 'CITYSCAPE':
      imdb = cityscape(FLAGS.image_set, FLAGS.data_path, mc)
      print("Margins for Training data:", imdb.left_margin, imdb.top_margin, imdb.right_margin, imdb.bottom_margin)
      if FLAGS.eval_valid:
        imdb_valid = cityscape('val', FLAGS.data_path, mc)
        imdb_valid.mc.DATA_AUGMENTATION = False
        print("Margins for Validation data:", imdb_valid.left_margin, imdb_valid.top_margin, imdb_valid.right_margin, imdb_valid.bottom_margin)

    print("Training model data augmentation:", imdb.mc.DATA_AUGMENTATION)
    if imdb_valid != None:
      print("Validation model data augmentation:", imdb_valid.mc.DATA_AUGMENTATION)
    # save model size, flops, activations by layers
    with open(os.path.join(FLAGS.train_dir, 'model_metrics.txt'), 'w') as f:
      f.write('Number of parameter by layer:\n')
      count = 0
      for c in model.model_size_counter:
        f.write('\t{}: {}\n'.format(c[0], c[1]))
        count += c[1]
      f.write('\ttotal: {}\n'.format(count))

      count = 0
      f.write('\nActivation size by layer:\n')
      for c in model.activation_counter:
        f.write('\t{}: {}\n'.format(c[0], c[1]))

        count += c[1]
      f.write('\ttotal: {}\n'.format(count))

      count = 0
      f.write('\nNumber of flops by layer:\n')
      for c in model.flop_counter:
        f.write('\t{}: {}\n'.format(c[0], c[1]))
        count += c[1]
      f.write('\ttotal: {}\n'.format(count))
    f.close()
    print ('Model statistics saved to {}.'.format(
      os.path.join(FLAGS.train_dir, 'model_metrics.txt')))

    def _load_data(load_to_placeholder=True, eval_valid=False):
      # read batch input
      if eval_valid:
        # Only for validation set
        image_per_batch, label_per_batch, box_delta_per_batch, aidx_per_batch, \
          bbox_per_batch, edge_adhesions_per_batch = imdb_valid.read_batch(shuffle=False, wrap_around=False)
        keep_prob_value = 1.0
      else:
        image_per_batch, label_per_batch, box_delta_per_batch, aidx_per_batch, \
            bbox_per_batch, edge_adhesions_per_batch = imdb.read_batch()
        keep_prob_value = mc.DROP_OUT_PROB

      label_indices, bbox_indices, box_delta_values, mask_indices, box_values, edge_adhesions, edge_indices\
          = [], [], [], [], [], [], []
      aidx_set = set()
      num_discarded_labels = 0
      num_labels = 0
      for i in range(len(label_per_batch)): # batch_size
        for j in range(len(label_per_batch[i])): # number of annotations
          num_labels += 1
          if (i, aidx_per_batch[i][j]) not in aidx_set:
            aidx_set.add((i, aidx_per_batch[i][j]))
            label_indices.append(
                [i, aidx_per_batch[i][j], label_per_batch[i][j]])
            mask_indices.append([i, aidx_per_batch[i][j]])
            bbox_indices.extend(
                [[i, aidx_per_batch[i][j], k] for k in range(FLAGS.mask_parameterization)])
            box_delta_values.extend(box_delta_per_batch[i][j])
            box_values.extend(bbox_per_batch[i][j])
            edge_adhesions.extend(edge_adhesions_per_batch[i][j])
            edge_indices.extend(
                [[i, aidx_per_batch[i][j], k] for k in range(FLAGS.mask_parameterization)])
          else:
            num_discarded_labels += 1

      if mc.DEBUG_MODE:
        print ('Warning: Discarded {}/({}) labels that are assigned to the same '
               'anchor'.format(num_discarded_labels, num_labels))

      if load_to_placeholder:
        image_input = model.ph_image_input
        input_mask = model.ph_input_mask
        box_delta_input = model.ph_box_delta_input
        box_input = model.ph_box_input
        labels = model.ph_labels
        edge_scenarios = model.ph_edge_adhesions
      else:
        image_input = model.image_input
        input_mask = model.input_mask
        box_delta_input = model.box_delta_input
        box_input = model.box_input
        labels = model.labels
        edge_scenarios = model.edge_adhesions

      feed_dict = {
          image_input: image_per_batch,
          input_mask: np.reshape(
              sparse_to_dense(
                  mask_indices, [mc.BATCH_SIZE, mc.ANCHORS],
                  [1.0]*len(mask_indices)),
              [mc.BATCH_SIZE, mc.ANCHORS, 1]),
          box_delta_input: sparse_to_dense(
              bbox_indices, [mc.BATCH_SIZE, mc.ANCHORS, FLAGS.mask_parameterization],
              box_delta_values),
          box_input: sparse_to_dense(
              bbox_indices, [mc.BATCH_SIZE, mc.ANCHORS, FLAGS.mask_parameterization],
              box_values),
          labels: sparse_to_dense(
              label_indices,
              [mc.BATCH_SIZE, mc.ANCHORS, mc.CLASSES],
              [1.0]*len(label_indices)),
          model.keep_prob: keep_prob_value,
          edge_scenarios: sparse_to_dense(
              edge_indices, [mc.BATCH_SIZE, mc.ANCHORS, FLAGS.mask_parameterization],
              edge_adhesions).astype(np.bool, copy=False),
      }

      return feed_dict, image_per_batch, label_per_batch, bbox_per_batch, edge_indices


    def _enqueue(sess, coord):
      try:
        while not coord.should_stop():
          feed_dict, _, _, _, _ = _load_data()
          sess.run(model.enqueue_op, feed_dict=feed_dict)
          if mc.DEBUG_MODE:
            print ("added to the queue")
        if mc.DEBUG_MODE:
          print ("Finished enqueue")
      except tf.errors.CancelledError:
        coord.request_stop()

    sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True))

    saver = tf.train.Saver(tf.global_variables())
    summary_op = tf.summary.merge_all()

    init = tf.global_variables_initializer()
    sess.run(init)
    glb_step = sess.run(model.global_step)
    print("Global step before restore:", glb_step)

    print("Kernels before restore")
    for v in tf.trainable_variables():
      if 'kernels' in v.name:
        print("First few weights of ", v.name, " are ", sess.run(v)[0,0,0,0:5])

    print("Learning rate before restore", sess.run(model.lr))

    ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir)
    if ckpt and ckpt.model_checkpoint_path:
      print("Found checkpoint at step: ", int(ckpt.model_checkpoint_path.split('/')[-1].split('-')[-1]))
      last_layer_name = model.preds.name.split('/')[0]
      if FLAGS.mask_parameterization == 8 and FLAGS.bounding_box_checkpoint:
        print("Loading only partial weights (except last layer", last_layer_name, ")")
        saver_partial_weights = tf.train.Saver([v for v in tf.global_variables() if last_layer_name not in v.name])
        saver_partial_weights.restore(sess, ckpt.model_checkpoint_path)
        if FLAGS.warm_restart_lr != -1.0:
          print("Resetting global step")
          sess.run([model.global_step.assign(0)])
      else:
        print("Loading all weights (including the last layer", last_layer_name, ")")
        saver.restore(sess, ckpt.model_checkpoint_path)
    else:
      print("Checkpoint not found !")
    glb_step = sess.run(model.global_step)
    print("Global step after restore:", glb_step)
    
    print("Kernels after restore")
    for v in tf.trainable_variables():
      if 'kernels' in v.name:
        print("First few weights of ", v.name, " are ", sess.run(v)[0,0,0,0:5])

    print("Learning rate after restore", sess.run(model.lr))

    summary_writer = tf.summary.FileWriter(FLAGS.train_dir, sess.graph)
    with open(os.path.join(FLAGS.train_dir, 'training_metrics.txt'), 'a') as f:
      f.write("Global step after restore: "+str(glb_step)+"\n")
    f.close()
    if FLAGS.eval_valid:
      with open(os.path.join(FLAGS.train_dir, 'validation_metrics.txt'), 'a') as f:
        f.write("Global step after restore: "+str(glb_step)+"\n")
      f.close()
    coord = tf.train.Coordinator()

    if mc.NUM_THREAD > 0:
      enq_threads = []
      for _ in range(mc.NUM_THREAD):
        enq_thread = threading.Thread(target=_enqueue, args=[sess, coord])
        # enq_thread.isDaemon()
        enq_thread.start()
        enq_threads.append(enq_thread)

    threads = tf.train.start_queue_runners(coord=coord, sess=sess)
    run_options = tf.RunOptions(timeout_in_ms=60000)

    try: 
      for step in xrange(glb_step, FLAGS.max_steps):
        if coord.should_stop():
          sess.run(model.FIFOQueue.close(cancel_pending_enqueues=True))
          coord.request_stop()
          coord.join(threads)
          break

        start_time = time.time()

        if step % FLAGS.summary_step == 0:
          feed_dict, image_per_batch, label_per_batch, bbox_per_batch, edge_ids = \
              _load_data(load_to_placeholder=False)
          op_list = [
              model.train_op, model.loss, summary_op, model.det_boxes,
              model.det_probs, model.det_class, model.conf_loss,
              model.bbox_loss, model.class_loss, model.edge_adhesions,
          ]
          _, loss_value, summary_str, det_boxes, det_probs, det_class, \
              conf_loss, bbox_loss, class_loss, edge_adhesions_pre_filtered = sess.run(
                  op_list, feed_dict=feed_dict)

          summary_writer.add_summary(summary_str, step)
          # Visualize the training examples only if validation is not enabled
          if not FLAGS.eval_valid:
            visualize_gt_masks = False
            visualize_pred_masks = False
            if mc.EIGHT_POINT_REGRESSION:
              visualize_gt_masks = True
              visualize_pred_masks = True

            assert np.array_equal(feed_dict[model.edge_adhesions], edge_adhesions_pre_filtered), \
                "Training Gt edge adhesion not matching edge adhesion tensor" 
            edge_adhesions_per_batch = [[0]]*mc.BATCH_SIZE
            for id_val in range(mc.BATCH_SIZE):
              selected_ids = np.where(np.asarray(edge_ids)[:,0] == id_val)[0]
              # print("Before",np.asarray(edge_ids)[selected_ids][:,1])
              indexes_int = np.unique(np.asarray(edge_ids)[selected_ids][:,1], return_index=True)[1]
              anchors_ids = np.asarray([np.asarray(edge_ids)[selected_ids][:,1][index] for index in sorted(indexes_int)])
              # print("After",anchors_ids)
              batch_id = [id_val]*len(anchors_ids)
              edge_adhesions_per_batch[id_val] = edge_adhesions_pre_filtered[batch_id, anchors_ids]

            _viz_prediction_result(
                model, image_per_batch, bbox_per_batch, label_per_batch, det_boxes,
                det_class, det_probs, visualize_gt_masks, visualize_pred_masks)
            image_per_batch = bgr_to_rgb(image_per_batch)
            viz_summary = sess.run(
                model.viz_op, feed_dict={model.image_to_show: image_per_batch})
            summary_writer.add_summary(viz_summary, step)
          
          print ('total_loss: {}, conf_loss: {}, bbox_loss: {}, class_loss: {}'.\
                format(loss_value, conf_loss, bbox_loss, class_loss))
          with open(os.path.join(FLAGS.train_dir, 'training_metrics.txt'), 'a') as f:
            f.write('step: {}, total_loss: {}, conf_loss: {}, bbox_loss: {}, class_loss: {}\n'.\
                format(step, loss_value, conf_loss, bbox_loss, class_loss))
          f.close()
          if FLAGS.eval_valid:
            print ('\n!! Validation Set evaluation at step ', step, ' !!')
            with open(os.path.join(FLAGS.train_dir, 'validation_metrics.txt'), 'a') as f:
              f.write('\n!! Validation Set evaluation at step '+str(step)+' !!\n')
              loss_list = []
              batch_nr = 0
              while True:
                batch_nr += 1
                if len(imdb_valid._image_idx) % mc.BATCH_SIZE > 0:
                  # if batch_size unevenly divides the number of samples.
                  # then number of batches is one more than the actual num of batches
                  num_of_batches = (len(imdb_valid._image_idx) // mc.BATCH_SIZE) + 1
                else:
                  num_of_batches = (len(imdb_valid._image_idx) // mc.BATCH_SIZE)
                if batch_nr > num_of_batches:
                  break
                feed_dict_val, image_per_batch_val, label_per_batch_val, bbox_per_batch_val, edge_ids_val = \
                    _load_data(load_to_placeholder=False, eval_valid=True)
                op_list_val = [
                    model.loss, model.conf_loss, model.bbox_loss, \
                    model.class_loss, model.det_boxes, \
                    model.det_probs, model.det_class,
                    model.edge_adhesions,
                ]
                loss_value_val, conf_loss_val, bbox_loss_val, class_loss_val, det_boxes_val, \
                  det_probs_val, det_class_val, edge_adhesions_pre_filtered_val = sess.run(op_list_val, feed_dict=feed_dict_val)

                if batch_nr == 1:
                  # Sample the first batch for visualization
                  visualize_gt_masks = False
                  visualize_pred_masks = False
                  if mc.EIGHT_POINT_REGRESSION:
                    visualize_gt_masks = True
                    visualize_pred_masks = True

                  assert np.array_equal(feed_dict_val[model.edge_adhesions], edge_adhesions_pre_filtered_val), \
                          "Validation Gt edge adhesion not matching edge adhesion tensor"
                  edge_adhesions_per_batch_val = [[0]]*mc.BATCH_SIZE
                  for id_val in range(mc.BATCH_SIZE):
                    selected_ids = np.where(np.asarray(edge_ids_val)[:,0] == id_val)[0]
                    indexes_int = np.unique(np.asarray(edge_ids_val)[selected_ids][:,1], return_index=True)[1]
                    anchors_ids = np.asarray([np.asarray(edge_ids_val)[selected_ids][:,1][index] for index in sorted(indexes_int)])
                    batch_id = [id_val]*len(anchors_ids)
                    edge_adhesions_per_batch_val[id_val] = edge_adhesions_pre_filtered_val[batch_id, anchors_ids]

                  _viz_prediction_result(
                      model, image_per_batch_val, bbox_per_batch_val, label_per_batch_val, det_boxes_val,
                      det_class_val, det_probs_val, visualize_gt_masks, visualize_pred_masks)
                  image_per_batch_visualize = bgr_to_rgb(image_per_batch_val)

                loss_list.append([loss_value_val, conf_loss_val, bbox_loss_val, class_loss_val])
                f.write('Batch: {}, total_loss: {}, conf_loss: {}, bbox_loss: {}, class_loss: {}\n'.\
                        format(batch_nr, loss_value_val, conf_loss_val, bbox_loss_val, class_loss_val))
              loss_list = np.asarray(loss_list)
              loss_means = [np.mean(loss_list[:,0]), np.mean(loss_list[:,1]), np.mean(loss_list[:,2]), np.mean(loss_list[:,3])]
              loss_stds = [np.std(loss_list[:,0]), np.std(loss_list[:,1]), np.std(loss_list[:,2]), np.std(loss_list[:,3])]
              print ('Mean values : total_loss: {}, conf_loss: {}, bbox_loss: {}, class_loss: {}'.\
                format(loss_means[0], loss_means[1], loss_means[2], loss_means[3]))
              print ('Standard Deviation values : total_loss: {}, conf_loss: {}, bbox_loss: {}, class_loss: {}'.\
                format(loss_stds[0], loss_stds[1], loss_stds[2], loss_stds[3]))
              f.write('Mean values : total_loss: {}, conf_loss: {}, bbox_loss: {}, class_loss: {}\n'.\
                format(loss_means[0], loss_means[1], loss_means[2], loss_means[3]))
              f.write('Standard Deviation values : total_loss: {}, conf_loss: {}, bbox_loss: {}, class_loss: {}\n'.\
                format(loss_stds[0], loss_stds[1], loss_stds[2], loss_stds[3]))
              # Visualize the validation examples
              if len(image_per_batch_visualize) != 0:
                viz_summary = sess.run(
                    model.viz_op, feed_dict={model.image_to_show: image_per_batch_visualize})
                summary_writer.add_summary(viz_summary, step)
            f.close()
          summary_writer.flush()
        else:
          if mc.NUM_THREAD > 0:
            _, loss_value, conf_loss, bbox_loss, class_loss = sess.run(
                [model.train_op, model.loss, model.conf_loss, model.bbox_loss,
                 model.class_loss], options=run_options)
          else:
            feed_dict, _, _, _, _ = _load_data(load_to_placeholder=False)
            _, loss_value, conf_loss, bbox_loss, class_loss = sess.run(
                [model.train_op, model.loss, model.conf_loss, model.bbox_loss,
                 model.class_loss], feed_dict=feed_dict)

        duration = time.time() - start_time

        assert not np.isnan(loss_value), \
            'Model diverged. Total loss: {}, conf_loss: {}, bbox_loss: {}, ' \
            'class_loss: {}'.format(loss_value, conf_loss, bbox_loss, class_loss)

        if step % 10 == 0:
          num_images_per_step = mc.BATCH_SIZE
          images_per_sec = num_images_per_step / duration
          sec_per_batch = float(duration)
          format_str = ('%s: step %d, loss = %.2f (%.1f images/sec; %.3f '
                        'sec/batch)')
          print (format_str % (datetime.now(), step, loss_value,
                               images_per_sec, sec_per_batch))
          with open(os.path.join(FLAGS.train_dir, 'training_metrics.txt'), 'a') as f:
            f.write(format_str % (datetime.now(), step, loss_value,
                               images_per_sec, sec_per_batch) + '\n')
          f.close()
          sys.stdout.flush()

        # Save the model checkpoint periodically.
        if step % FLAGS.checkpoint_step == 0 or (step + 1) == FLAGS.max_steps:
          checkpoint_path = os.path.join(FLAGS.train_dir, 'model.ckpt')
          print("Checkpointing at ", step)
          saver.save(sess, checkpoint_path, global_step=step)
      sess.run(model.FIFOQueue.close(cancel_pending_enqueues=True))
      coord.request_stop()
      coord.join(threads)
    except KeyboardInterrupt:
      print("Keyboard interrupt caught ! Terminating..")
      sess.run(model.FIFOQueue.close(cancel_pending_enqueues=True))
      coord.request_stop()
      coord.join(threads)
      sys.exit(0)
    except:
      print("Unexpected error:", sys.exc_info()[0])
      sess.run(model.FIFOQueue.close(cancel_pending_enqueues=True))
      coord.request_stop()
      coord.join(threads)
      sys.exit(0)

def main(argv=None):  # pylint: disable=unused-argument
  if not tf.gfile.Exists(FLAGS.train_dir):
    # tf.gfile.DeleteRecursively(FLAGS.train_dir)
    tf.gfile.MakeDirs(FLAGS.train_dir)
  train()


if __name__ == '__main__':
  tf.app.run()
