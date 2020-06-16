import argparse
from collections import OrderedDict

import torch


def convert(src, dst):
    """Convert keys in pycls pretrained EfficientNet models to mmdet style."""
    # load pycls model
    efficientnet_model = torch.load(src)
    # convert to pytorch style
    new_model_state = OrderedDict()
    pycls_to_mmdet = {
        **{f"b{i}": f"{i - 1}" for i in range(20)},
        **{f"s{i}": f"layer{i}" for i in range(20)},
        "stem.conv": "conv1",
        "stem.bn": "bn1",
        "dwise_bn": "bndwise",
        "lin_proj_bn": "bnlin_proj",
        "exp_bn": "bnexp",
    }
    for k, v in efficientnet_model["model_state"].items():
        old_k = k
        migrated = False
        for pycls_name, mmdet_name in pycls_to_mmdet.items():
            if pycls_name in k:
                migrated = True
                k = k.replace(pycls_name, mmdet_name)
        if migrated:
            print("Old:", old_k)
            print("New:", k)
            new_model_state[k] = v
        else:
            print("Not migrated:", old_k)

    # save checkpoint
    checkpoint = dict()
    checkpoint['state_dict'] = new_model_state
    torch.save(checkpoint, dst)


def main():
    parser = argparse.ArgumentParser(description='Convert model keys')
    parser.add_argument('src', help='src pycls efficientnet model path')
    parser.add_argument('dst', help='save path')
    args = parser.parse_args()
    convert(args.src, args.dst)


if __name__ == '__main__':
    main()
