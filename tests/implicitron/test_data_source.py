# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import os
import unittest

from omegaconf import OmegaConf
from pytorch3d.implicitron.dataset.data_source import ImplicitronDataSource
from pytorch3d.implicitron.tools.config import get_default_args

if os.environ.get("FB_TEST", False):
    from common_testing import get_tests_dir
else:
    from tests.common_testing import get_tests_dir

DATA_DIR = get_tests_dir() / "implicitron/data"
DEBUG: bool = False


class TestDataSource(unittest.TestCase):
    def setUp(self):
        self.maxDiff = None

    def test_one(self):
        cfg = get_default_args(ImplicitronDataSource)
        yaml = OmegaConf.to_yaml(cfg, sort_keys=False)
        if DEBUG:
            (DATA_DIR / "data_source.yaml").write_text(yaml)
        self.assertEqual(yaml, (DATA_DIR / "data_source.yaml").read_text())
