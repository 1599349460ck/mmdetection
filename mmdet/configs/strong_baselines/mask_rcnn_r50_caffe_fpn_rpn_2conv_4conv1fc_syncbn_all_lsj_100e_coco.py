if '_base_':
    from .._base_.models.mask_rcnn_r50_fpn import *
    from ..common.lsj_100e_coco_instance import *
from mmdet.models.data_preprocessors.data_preprocessor import BatchFixedSizePad
from mmdet.models.roi_heads.bbox_heads.convfc_bbox_head import Shared4Conv1FCBBoxHead
from mmcv.transforms.loading import LoadImageFromFile, LoadImageFromFile
from mmdet.datasets.transforms.loading import LoadAnnotations, FilterAnnotations, LoadAnnotations
from mmcv.transforms.processing import RandomResize
from mmdet.datasets.transforms.transforms import RandomCrop, RandomFlip, Resize
from mmdet.datasets.transforms.formatting import PackDetInputs, PackDetInputs

image_size = (1024, 1024)
batch_augments = [dict(type=BatchFixedSizePad, size=image_size, pad_mask=True)]
norm_cfg = dict(type='SyncBN', requires_grad=True)
# Use MMSyncBN that handles empty tensor in head. It can be changed to
# SyncBN after https://github.com/pytorch/pytorch/issues/36530 is fixed
head_norm_cfg = dict(type='MMSyncBN', requires_grad=True)
model.merge(
    dict(
        # use caffe norm
        data_preprocessor=dict(
            mean=[103.530, 116.280, 123.675],
            std=[1.0, 1.0, 1.0],
            bgr_to_rgb=False,

            # pad_size_divisor=32 is unnecessary in training but necessary
            # in testing.
            pad_size_divisor=32,
            batch_augments=batch_augments),
        backbone=dict(
            frozen_stages=-1,
            norm_eval=False,
            norm_cfg=norm_cfg,
            init_cfg=None,
            style='caffe'),
        neck=dict(norm_cfg=norm_cfg),
        rpn_head=dict(num_convs=2),
        roi_head=dict(
            bbox_head=dict(
                type=Shared4Conv1FCBBoxHead,
                conv_out_channels=256,
                norm_cfg=head_norm_cfg),
            mask_head=dict(norm_cfg=head_norm_cfg))))

train_pipeline = [
    dict(type=LoadImageFromFile, backend_args=backend_args),
    dict(type=LoadAnnotations, with_bbox=True, with_mask=True),
    dict(
        type=RandomResize,
        scale=image_size,
        ratio_range=(0.1, 2.0),
        keep_ratio=True),
    dict(
        type=RandomCrop,
        crop_type='absolute_range',
        crop_size=image_size,
        recompute_bbox=True,
        allow_negative_crop=True),
    dict(type=FilterAnnotations, min_gt_bbox_wh=(1e-2, 1e-2)),
    dict(type=RandomFlip, prob=0.5),
    dict(type=PackDetInputs)
]
test_pipeline = [
    dict(type=LoadImageFromFile, backend_args=backend_args),
    dict(type=Resize, scale=(1333, 800), keep_ratio=True),
    dict(type=LoadAnnotations, with_bbox=True, with_mask=True),
    dict(
        type=PackDetInputs,
        meta_keys=('img_id', 'img_path', 'ori_shape', 'img_shape',
                   'scale_factor'))
]

# Use RepeatDataset to speed up training
train_dataloader.merge(
    dict(dataset=dict(dataset=dict(pipeline=train_pipeline))))
