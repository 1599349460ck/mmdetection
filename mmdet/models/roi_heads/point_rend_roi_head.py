# Modified from https://github.com/facebookresearch/detectron2/tree/master/projects/PointRend  # noqa
import os

import torch
import torch.nn.functional as F
from mmcv.ops import point_sample, rel_roi_point_to_rel_img_point

from mmdet.core import bbox2roi, bbox_mapping, merge_aug_masks
from .. import builder
from ..builder import HEADS
from .standard_roi_head import StandardRoIHead


@HEADS.register_module()
class PointRendRoIHead(StandardRoIHead):
    """`PointRend <https://arxiv.org/abs/1912.08193>`_."""

    def __init__(self, point_head, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert self.with_bbox and self.with_mask
        self.init_point_head(point_head)

    def init_point_head(self, point_head):
        """Initialize ``point_head``"""
        self.point_head = builder.build_head(point_head)

    def init_weights(self, pretrained):
        """Initialize the weights in head.

        Args:
            pretrained (str, optional): Path to pre-trained weights.
        """
        super().init_weights(pretrained)
        self.point_head.init_weights()

    def _mask_forward_train(self, x, sampling_results, bbox_feats, gt_masks,
                            img_metas):
        """Run forward function and calculate loss for mask head and point head
        in training."""
        mask_results = super()._mask_forward_train(x, sampling_results,
                                                   bbox_feats, gt_masks,
                                                   img_metas)
        if mask_results['loss_mask'] is not None:
            loss_point = self._mask_point_forward_train(
                x, sampling_results, mask_results['mask_pred'], gt_masks,
                img_metas)
            mask_results['loss_mask'].update(loss_point)

        return mask_results

    def _mask_point_forward_train(self, x, sampling_results, mask_pred,
                                  gt_masks, img_metas):
        """Run forward function and calculate loss for point head in
        training."""
        pos_labels = torch.cat([res.pos_gt_labels for res in sampling_results])
        rel_roi_points = self.point_head.get_roi_rel_points_train(
            mask_pred, pos_labels, cfg=self.train_cfg)
        rois = bbox2roi([res.pos_bboxes for res in sampling_results])

        fine_grained_point_feats = self._get_fine_grained_point_feats(
            x, rois, rel_roi_points, img_metas)
        coarse_point_feats = point_sample(mask_pred, rel_roi_points)
        mask_point_pred = self.point_head(fine_grained_point_feats,
                                          coarse_point_feats)
        mask_point_target = self.point_head.get_targets(
            rois, rel_roi_points, sampling_results, gt_masks, self.train_cfg)
        loss_mask_point = self.point_head.loss(mask_point_pred,
                                               mask_point_target, pos_labels)

        return loss_mask_point

    def _get_fine_grained_point_feats(self, x, rois, rel_roi_points,
                                      img_metas):
        """Sample fine grained feats from each level feature map and
        concatenate them together."""
        num_imgs = len(img_metas)
        batch_size = x[0].shape[0]
        num_rois = rois.shape[0]
        fine_grained_feats = []
        for idx in range(self.mask_roi_extractor.num_inputs):
            feats = x[idx]
            spatial_scale = 1. / float(
                self.mask_roi_extractor.featmap_strides[idx])
            # support export to ONNX with batch dim
            if torch.onnx.is_in_onnx_export():
                rel_img_points = rel_roi_point_to_rel_img_point(
                    rois, rel_roi_points, feats, spatial_scale)
                channels = feats.shape[1]
                num_points = rel_img_points.shape[1]
                rel_img_points = rel_img_points.reshape(
                    batch_size, -1, num_points, 2)
                point_feats = point_sample(feats, rel_img_points)
                point_feats = point_feats.transpose(1, 2).reshape(
                    num_rois, channels, num_points)
                fine_grained_feats.append(point_feats)
            else:
                point_feats = []
                for batch_ind in range(num_imgs):
                    # unravel batch dim
                    feat = feats[batch_ind].unsqueeze(0)
                    inds = (rois[:, 0].long() == batch_ind)
                    if inds.any():
                        rel_img_points = rel_roi_point_to_rel_img_point(
                            rois[inds], rel_roi_points[inds], feat,
                            spatial_scale).unsqueeze(0)
                        point_feat = point_sample(feat, rel_img_points)
                        point_feat = point_feat.squeeze(0).transpose(0, 1)
                        point_feats.append(point_feat)
                fine_grained_feats.append(torch.cat(point_feats, dim=0))
        return torch.cat(fine_grained_feats, dim=1)

    def _mask_point_forward_test(self, x, rois, label_pred, mask_pred,
                                 img_metas):
        """Mask refining process with point head in testing."""
        refined_mask_pred = mask_pred.clone()
        for subdivision_step in range(self.test_cfg.subdivision_steps):
            refined_mask_pred = F.interpolate(
                refined_mask_pred,
                scale_factor=self.test_cfg.scale_factor,
                mode='bilinear',
                align_corners=False)
            # If `subdivision_num_points` is larger or equal to the
            # resolution of the next step, then we can skip this step
            num_rois, channels, mask_height, mask_width = \
                refined_mask_pred.shape
            if (self.test_cfg.subdivision_num_points >=
                    self.test_cfg.scale_factor**2 * mask_height * mask_width
                    and
                    subdivision_step < self.test_cfg.subdivision_steps - 1):
                continue
            point_indices, rel_roi_points = \
                self.point_head.get_roi_rel_points_test(
                    refined_mask_pred, label_pred, cfg=self.test_cfg)
            fine_grained_point_feats = self._get_fine_grained_point_feats(
                x, rois, rel_roi_points, img_metas)
            coarse_point_feats = point_sample(mask_pred, rel_roi_points)
            mask_point_pred = self.point_head(fine_grained_point_feats,
                                              coarse_point_feats)

            point_indices = point_indices.unsqueeze(1).expand(-1, channels, -1)
            refined_mask_pred = refined_mask_pred.reshape(
                num_rois, channels, mask_height * mask_width)

            is_trt_backend = os.environ.get('ONNX_BACKEND') == 'MMCVTensorRT'
            # avoid ScatterElements op in ONNX for TensorRT
            if torch.onnx.is_in_onnx_export() and is_trt_backend:
                mask_shape = refined_mask_pred.shape
                point_shape = point_indices.shape
                inds_dim0 = torch.arange(point_shape[0]).reshape(
                    point_shape[0], 1, 1).expand_as(point_indices)
                inds_dim1 = torch.arange(point_shape[1]).reshape(
                    1, point_shape[1], 1).expand_as(point_indices)
                inds_1d = inds_dim0.reshape(
                    -1) * mask_shape[1] * mask_shape[2] + inds_dim1.reshape(
                        -1) * mask_shape[2] + point_indices.reshape(-1)
                refined_mask_pred = refined_mask_pred.reshape(-1)
                refined_mask_pred[inds_1d] = mask_point_pred.reshape(-1)
                refined_mask_pred = refined_mask_pred.reshape(*mask_shape)
            else:
                refined_mask_pred = refined_mask_pred.scatter_(
                    2, point_indices, mask_point_pred)

            refined_mask_pred = refined_mask_pred.view(num_rois, channels,
                                                       mask_height, mask_width)

        return refined_mask_pred

    def simple_test_mask(self,
                         x,
                         img_metas,
                         det_bboxes,
                         det_labels,
                         rescale=False):
        """Obtain mask prediction without augmentation."""
        ori_shapes = tuple(meta['ori_shape'] for meta in img_metas)
        scale_factors = tuple(meta['scale_factor'] for meta in img_metas)
        num_imgs = len(det_bboxes)
        if all(det_bbox.shape[0] == 0 for det_bbox in det_bboxes):
            if torch.onnx.is_in_onnx_export():
                raise RuntimeError('[ONNX Error] Can not record MaskHead '
                                   'as it has not been executed this time')
            segm_results = [[[] for _ in range(self.mask_head.num_classes)]
                            for _ in range(num_imgs)]
        else:
            # The length of proposals of different batches may be different.
            # In order to form a batch, a padding operation is required.
            if isinstance(det_bboxes, list):
                # padding to form a batch
                max_size = max([bboxes.size(0) for bboxes in det_bboxes])
                for i, (bbox, label) in enumerate(zip(det_bboxes, det_labels)):
                    supplement_bbox = bbox.new_full(
                        (max_size - bbox.size(0), bbox.size(1)), 0)
                    supplement_label = label.new_full(
                        (max_size - label.size(0), ), 0)
                    det_bboxes[i] = torch.cat((supplement_bbox, bbox), dim=0)
                    det_labels[i] = torch.cat((supplement_label, label), dim=0)
                det_bboxes = torch.stack(det_bboxes, dim=0)
                det_labels = torch.stack(det_labels, dim=0)

            batch_size = det_bboxes.size(0)
            num_proposals_per_img = det_bboxes.shape[1]

            # if det_bboxes is rescaled to the original image size, we need to
            # rescale it back to the testing scale to obtain RoIs.
            det_bboxes = det_bboxes[..., :4]
            if rescale:
                if not isinstance(scale_factors[0], float):
                    scale_factors = det_bboxes.new_tensor(scale_factors)
                det_bboxes = det_bboxes * scale_factors.unsqueeze(1)
            batch_index = torch.arange(
                det_bboxes.size(0),
                device=det_bboxes.device).float().view(-1, 1, 1).expand(
                    det_bboxes.size(0), det_bboxes.size(1), 1)
            mask_rois = torch.cat([batch_index, det_bboxes], dim=-1)
            mask_rois = mask_rois.view(-1, 5)
            mask_results = self._mask_forward(x, mask_rois)
            mask_pred = mask_results['mask_pred']

            # Support exporting to ONNX
            if torch.onnx.is_in_onnx_export():
                max_shape = img_metas[0]['img_shape_for_onnx']
                num_det = det_bboxes.shape[1]
                det_bboxes = det_bboxes.reshape(-1, 4)
                det_labels = det_labels.reshape(-1)

                mask_pred = self._mask_point_forward_test(
                    x, mask_rois, det_labels, mask_pred, img_metas)

                segm_results = self.mask_head.get_seg_masks(
                    mask_pred, det_bboxes, det_labels, self.test_cfg,
                    max_shape, scale_factors[0], rescale)
                segm_results = segm_results.reshape(batch_size, num_det,
                                                    max_shape[0], max_shape[1])
                return segm_results

            # Recover the batch dimension
            mask_preds = mask_pred.reshape(batch_size, num_proposals_per_img,
                                           *mask_pred.shape[1:])
            mask_rois = mask_rois.view(batch_size, -1, 5)

            # apply mask post-processing to each image individually
            segm_results = []
            for i in range(num_imgs):
                mask_pred = mask_preds[i]
                det_bbox = det_bboxes[i]
                det_label = det_labels[i]
                mask_rois_i = mask_rois[i]

                # remove padding
                supplement_mask = det_bbox[..., -1] != 0
                mask_pred = mask_pred[supplement_mask]
                det_bbox = det_bbox[supplement_mask]
                det_label = det_label[supplement_mask]

                if det_label.shape[0] == 0:
                    segm_results.append(
                        [[] for _ in range(self.mask_head.num_classes)])
                else:
                    x_i = [xx[[i]] for xx in x]
                    if not torch.onnx.is_in_onnx_export():
                        mask_rois_i[:, 0] = 0  # TODO: remove this hack
                    mask_pred_i = self._mask_point_forward_test(
                        x_i, mask_rois_i, det_label, mask_pred, [img_metas])
                    segm_result = self.mask_head.get_seg_masks(
                        mask_pred_i, det_bbox, det_label, self.test_cfg,
                        ori_shapes[i], scale_factors[i], rescale)
                    segm_results.append(segm_result)
        return segm_results

    def aug_test_mask(self, feats, img_metas, det_bboxes, det_labels):
        """Test for mask head with test time augmentation."""
        if det_bboxes.shape[0] == 0:
            segm_result = [[] for _ in range(self.mask_head.num_classes)]
        else:
            aug_masks = []
            for x, img_meta in zip(feats, img_metas):
                img_shape = img_meta[0]['img_shape']
                scale_factor = img_meta[0]['scale_factor']
                flip = img_meta[0]['flip']
                _bboxes = bbox_mapping(det_bboxes[:, :4], img_shape,
                                       scale_factor, flip)
                mask_rois = bbox2roi([_bboxes])
                mask_results = self._mask_forward(x, mask_rois)
                mask_results['mask_pred'] = self._mask_point_forward_test(
                    x, mask_rois, det_labels, mask_results['mask_pred'],
                    img_metas)
                # convert to numpy array to save memory
                aug_masks.append(
                    mask_results['mask_pred'].sigmoid().cpu().numpy())
            merged_masks = merge_aug_masks(aug_masks, img_metas, self.test_cfg)

            ori_shape = img_metas[0][0]['ori_shape']
            segm_result = self.mask_head.get_seg_masks(
                merged_masks,
                det_bboxes,
                det_labels,
                self.test_cfg,
                ori_shape,
                scale_factor=1.0,
                rescale=False)
        return segm_result
