# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


import dataclasses
import os
from typing import Any, cast, Dict, List, Optional, Tuple

import lpips
import torch
from iopath.common.file_io import PathManager
from pytorch3d.implicitron.dataset.data_source import ImplicitronDataSource, Task
from pytorch3d.implicitron.dataset.dataset_base import DatasetBase, FrameData
from pytorch3d.implicitron.dataset.implicitron_dataset import ImplicitronDataset
from pytorch3d.implicitron.dataset.json_index_dataset_map_provider import (
    CO3D_CATEGORIES,
)
from pytorch3d.implicitron.dataset.utils import is_known_frame
from pytorch3d.implicitron.evaluation.evaluate_new_view_synthesis import (
    aggregate_nvs_results,
    eval_batch,
    pretty_print_nvs_metrics,
    summarize_nvs_eval_results,
)
from pytorch3d.implicitron.models.model_dbir import ModelDBIR
from pytorch3d.implicitron.tools.utils import dataclass_to_cuda_
from tqdm import tqdm


def main() -> None:
    """
    Evaluates new view synthesis metrics of a simple depth-based image rendering
    (DBIR) model for multisequence/singlesequence tasks for several categories.

    The evaluation is conducted on the same data as in [1] and, hence, the results
    are directly comparable to the numbers reported in [1].

    References:
        [1] J. Reizenstein, R. Shapovalov, P. Henzler, L. Sbordone,
                P. Labatut, D. Novotny:
            Common Objects in 3D: Large-Scale Learning
                and Evaluation of Real-life 3D Category Reconstruction
    """

    task_results = {}
    for task in (Task.SINGLE_SEQUENCE, Task.MULTI_SEQUENCE):
        task_results[task] = []
        for category in CO3D_CATEGORIES[: (20 if task == Task.SINGLE_SEQUENCE else 10)]:
            for single_sequence_id in (
                (0, 1) if task == Task.SINGLE_SEQUENCE else (None,)
            ):
                category_result = evaluate_dbir_for_category(
                    category, task=task, single_sequence_id=single_sequence_id
                )
                print("")
                print(
                    f"Results for task={task}; category={category};"
                    + (
                        f" sequence={single_sequence_id}:"
                        if single_sequence_id is not None
                        else ":"
                    )
                )
                pretty_print_nvs_metrics(category_result)
                print("")

                task_results[task].append(category_result)
            _print_aggregate_results(task, task_results)

    for task in task_results:
        _print_aggregate_results(task, task_results)


def evaluate_dbir_for_category(
    category: str,
    task: Task,
    bg_color: Tuple[float, float, float] = (0.0, 0.0, 0.0),
    single_sequence_id: Optional[int] = None,
    num_workers: int = 16,
    path_manager: Optional[PathManager] = None,
):
    """
    Evaluates new view synthesis metrics of a simple depth-based image rendering
    (DBIR) model for a given task, category, and sequence (in case task=='singlesequence').

    Args:
        category: Object category.
        bg_color: Background color of the renders.
        task: Evaluation task. Either singlesequence or multisequence.
        single_sequence_id: The ID of the evaluiation sequence for the singlesequence task.
        num_workers: The number of workers for the employed dataloaders.
        path_manager: (optional) Used for interpreting paths.

    Returns:
        category_result: A dictionary of quantitative metrics.
    """

    single_sequence_id = single_sequence_id if single_sequence_id is not None else -1

    torch.manual_seed(42)

    dataset_map_provider_args = {
        "category": category,
        "dataset_root": os.environ["CO3D_DATASET_ROOT"],
        "assert_single_seq": task == Task.SINGLE_SEQUENCE,
        "task_str": task.value,
        "test_on_train": False,
        "load_point_clouds": True,
        "test_restrict_sequence_id": single_sequence_id,
        "path_manager": path_manager,
    }
    data_source = ImplicitronDataSource(
        dataset_map_provider_JsonIndexDatasetMapProvider_args=dataset_map_provider_args
    )

    datasets, dataloaders = data_source.get_datasets_and_dataloaders()

    test_dataset = datasets.test
    test_dataloader = dataloaders.test
    if test_dataset is None or test_dataloader is None:
        raise ValueError("must have a test dataset.")

    if task == Task.SINGLE_SEQUENCE:
        # all_source_cameras are needed for evaluation of the
        # target camera difficulty
        # pyre-fixme[16]: `ImplicitronDataset` has no attribute `frame_annots`.
        sequence_name = test_dataset.frame_annots[0]["frame_annotation"].sequence_name
        all_source_cameras = _get_all_source_cameras(
            test_dataset, sequence_name, num_workers=num_workers
        )
    else:
        all_source_cameras = None

    image_size = cast(ImplicitronDataset, test_dataset).image_width

    if image_size is None:
        raise ValueError("Image size should be set in the dataset")

    # init the simple DBIR model
    model = ModelDBIR(  # pyre-ignore[28]: c’tor implicitly overridden
        render_image_width=image_size,
        render_image_height=image_size,
        bg_color=bg_color,
        max_points=int(1e5),
    )
    model.cuda()

    # init the lpips model for eval
    lpips_model = lpips.LPIPS(net="vgg")
    lpips_model = lpips_model.cuda()

    per_batch_eval_results = []
    print("Evaluating DBIR model ...")
    for frame_data in tqdm(test_dataloader):
        frame_data = dataclass_to_cuda_(frame_data)
        preds = model(**dataclasses.asdict(frame_data))
        per_batch_eval_results.append(
            eval_batch(
                frame_data,
                preds["implicitron_render"],
                bg_color=bg_color,
                lpips_model=lpips_model,
                source_cameras=all_source_cameras,
            )
        )

    category_result_flat, category_result = summarize_nvs_eval_results(
        per_batch_eval_results, task
    )

    return category_result["results"]


def _print_aggregate_results(
    task: Task, task_results: Dict[Task, List[List[Dict[str, Any]]]]
) -> None:
    """
    Prints the aggregate metrics for a given task.
    """
    aggregate_task_result = aggregate_nvs_results(task_results[task])
    print("")
    print(f"Aggregate results for task={task}:")
    pretty_print_nvs_metrics(aggregate_task_result)
    print("")


def _get_all_source_cameras(
    dataset: DatasetBase, sequence_name: str, num_workers: int = 8
):
    """
    Loads all training cameras of a given sequence.

    The set of all seen cameras is needed for evaluating the viewpoint difficulty
    for the singlescene evaluation.

    Args:
        dataset: Co3D dataset object.
        sequence_name: The name of the sequence.
        num_workers: The number of for the utilized dataloader.
    """

    # load all source cameras of the sequence
    seq_idx = list(dataset.sequence_indices_in_order(sequence_name))
    dataset_for_loader = torch.utils.data.Subset(dataset, seq_idx)
    (all_frame_data,) = torch.utils.data.DataLoader(
        dataset_for_loader,
        shuffle=False,
        batch_size=len(dataset_for_loader),
        num_workers=num_workers,
        collate_fn=FrameData.collate,
    )
    is_known = is_known_frame(all_frame_data.frame_type)
    source_cameras = all_frame_data.camera[torch.where(is_known)[0]]
    return source_cameras


if __name__ == "__main__":
    main()
