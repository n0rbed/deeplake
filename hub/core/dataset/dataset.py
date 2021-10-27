import pickle
import posixpath
import warnings
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import hub
import numpy as np
from hub.api.info import load_info
from hub.client.log import logger
from hub.constants import FIRST_COMMIT_ID
from hub.core.fast_forwarding import ffw_dataset_meta
from hub.core.index import Index
from hub.core.lock import lock, unlock
from hub.core.meta.dataset_meta import DatasetMeta
from hub.core.storage import LRUCache, S3Provider
from hub.core.tensor import Tensor, create_tensor
from hub.core.version_control.commit_node import CommitNode  # type: ignore
from hub.htype import DEFAULT_HTYPE, HTYPE_CONFIGURATIONS, UNSPECIFIED
from hub.integrations import dataset_to_tensorflow
from hub.util.bugout_reporter import hub_reporter
from hub.util.exceptions import (
    CouldNotCreateNewDatasetException,
    InvalidKeyTypeError,
    InvalidTensorGroupNameError,
    InvalidTensorNameError,
    LockedException,
    MemoryDatasetCanNotBePickledError,
    PathNotEmptyException,
    TensorAlreadyExistsError,
    TensorDoesNotExistError,
    TensorGroupAlreadyExistsError,
)
from hub.util.keys import (
    dataset_exists,
    get_dataset_info_key,
    get_dataset_meta_key,
    get_version_control_info_key,
    tensor_exists,
)
from hub.util.path import get_path_from_storage
from hub.util.remove_cache import get_base_storage
from hub.util.version_control import auto_checkout, checkout, commit, load_meta
from tqdm import tqdm  # type: ignore


class Dataset:
    def __init__(
        self,
        storage: LRUCache,
        index: Optional[Index] = None,
        group_index: str = "",
        read_only: bool = False,
        public: Optional[bool] = True,
        token: Optional[str] = None,
        verbose: bool = True,
        version_state: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        """Initializes a new or existing dataset.

        Args:
            storage (LRUCache): The storage provider used to access the dataset.
            index (Index, optional): The Index object restricting the view of this dataset's tensors.
            group_index (str): Name of the group this dataset instance represents.
            read_only (bool): Opens dataset in read only mode if this is passed as True. Defaults to False.
                Datasets stored on Hub cloud that your account does not have write access to will automatically open in read mode.
            public (bool, optional): Applied only if storage is Hub cloud storage and a new Dataset is being created. Defines if the dataset will have public access.
            token (str, optional): Activeloop token, used for fetching credentials for Hub datasets. This is optional, tokens are normally autogenerated.
            verbose (bool): If True, logs will be printed. Defaults to True.
            version_state (Dict[str, Any], optional): The version state of the dataset, includes commit_id, commit_node, branch, branch_commit_map and commit_node_map.
            **kwargs: Passing subclass variables through without errors.


        Raises:
            ValueError: If an existing local path is given, it must be a directory.
            ImproperDatasetInitialization: Exactly one argument out of 'path' and 'storage' needs to be specified.
                This is raised if none of them are specified or more than one are specifed.
            InvalidHubPathException: If a Hub cloud path (path starting with hub://) is specified and it isn't of the form hub://username/datasetname.
            AuthorizationException: If a Hub cloud path (path starting with hub://) is specified and the user doesn't have access to the dataset.
            PathNotEmptyException: If the path to the dataset doesn't contain a Hub dataset and is also not empty.
        """
        # uniquely identifies dataset
        self.path = get_path_from_storage(storage)
        self.storage = storage
        self._read_only = read_only
        base_storage = get_base_storage(storage)
        if (
            not read_only and index is None and isinstance(base_storage, S3Provider)
        ):  # Dataset locking only for S3 datasets
            try:
                lock(base_storage, callback=lambda: self._lock_lost_handler)
            except LockedException:
                self.read_only = True
                warnings.warn(
                    "Opening dataset in read only mode as another machine has locked it for writing."
                )

        self.index: Index = index or Index()
        self.group_index = group_index
        self._token = token
        self.public = public
        self.verbose = verbose
        self.version_state: Dict[str, Any] = version_state or {}
        self._set_derived_attributes()

    def _lock_lost_handler(self):
        """This is called when lock is acquired but lost later on due to slow update."""
        self.read_only = True
        warnings.warn(
            "Unable to update dataset lock as another machine has locked it for writing. Switching to read only mode."
        )

    def __enter__(self):
        self.storage.autoflush = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.storage.autoflush = True
        self.flush()

    @property
    def num_samples(self) -> int:
        """Returns the length of the smallest tensor.
        Ignores any applied indexing and returns the total length.
        """
        return min(map(len, self.version_state["full_tensors"].values()), default=0)

    @property
    def meta(self) -> DatasetMeta:
        """Returns the metadata of the dataset."""
        return self.version_state["meta"]

    def __len__(self):
        """Returns the length of the smallest tensor"""
        tensor_lengths = [len(tensor) for tensor in self.tensors.values()]
        return min(tensor_lengths, default=0)

    def __getstate__(self) -> Dict[str, Any]:
        """Returns a dict that can be pickled and used to restore this dataset.

        Note:
            Pickling a dataset does not copy the dataset, it only saves attributes that can be used to restore the dataset.
            If you pickle a local dataset and try to access it on a machine that does not have the data present, the dataset will not work.
        """
        if self.path.startswith("mem://"):
            raise MemoryDatasetCanNotBePickledError
        return {
            "path": self.path,
            "_read_only": self.read_only,
            "index": self.index,
            "group_index": self.group_index,
            "public": self.public,
            "storage": self.storage,
            "_token": self.token,
            "verbose": self.verbose,
            "version_state": self.version_state,
        }

    def __setstate__(self, state: Dict[str, Any]):
        """Restores dataset from a pickled state.

        Args:
            state (dict): The pickled state used to restore the dataset.
        """
        self.__dict__.update(state)
        self._set_derived_attributes()

    def __getitem__(
        self,
        item: Union[
            str, int, slice, List[int], Tuple[Union[int, slice, Tuple[int]]], Index
        ],
    ):
        if isinstance(item, str):
            fullpath = posixpath.join(self.group_index, item)
            tensor = self._get_tensor_from_root(fullpath)
            if tensor is not None:
                return tensor[self.index]
            elif self._has_group_in_root(fullpath):
                return self.__class__(
                    storage=self.storage,
                    index=self.index,
                    group_index=posixpath.join(self.group_index, item),
                    read_only=self.read_only,
                    token=self._token,
                    verbose=False,
                    version_state=self.version_state,
                    path=self.path,
                )
            elif "/" in item:
                splt = posixpath.split(item)
                return self[splt[0]][splt[1]]
            else:
                raise TensorDoesNotExistError(item)
        elif isinstance(item, (int, slice, list, tuple, Index)):
            return self.__class__(
                storage=self.storage,
                index=self.index[item],
                group_index=self.group_index,
                read_only=self.read_only,
                token=self._token,
                verbose=False,
                version_state=self.version_state,
                path=self.path,
            )
        else:
            raise InvalidKeyTypeError(item)

    @hub_reporter.record_call
    def create_tensor(
        self,
        name: str,
        htype: str = DEFAULT_HTYPE,
        dtype: Union[str, np.dtype] = UNSPECIFIED,
        sample_compression: str = UNSPECIFIED,
        chunk_compression: str = UNSPECIFIED,
        **kwargs,
    ):
        """Creates a new tensor in the dataset.

        Args:
            name (str): The name of the tensor to be created.
            htype (str): The class of data for the tensor.
                The defaults for other parameters are determined in terms of this value.
                For example, `htype="image"` would have `dtype` default to `uint8`.
                These defaults can be overridden by explicitly passing any of the other parameters to this function.
                May also modify the defaults for other parameters.
            dtype (str): Optionally override this tensor's `dtype`. All subsequent samples are required to have this `dtype`.
            sample_compression (str): All samples will be compressed in the provided format. If `None`, samples are uncompressed.
            chunk_compression (str): All chunks will be compressed in the provided format. If `None`, chunks are uncompressed.
            **kwargs: `htype` defaults can be overridden by passing any of the compatible parameters.
                To see all `htype`s and their correspondent arguments, check out `hub/htypes.py`.

        Returns:
            The new tensor, which can also be accessed by `self[name]`.

        Raises:
            TensorAlreadyExistsError: Duplicate tensors are not allowed.
            TensorGroupAlreadyExistsError: Duplicate tensor groups are not allowed.
            InvalidTensorNameError: If `name` is in dataset attributes.
            NotImplementedError: If trying to override `chunk_compression`.
        """
        # if not the head node, checkout to an auto branch that is newly created
        auto_checkout(self.version_state, self.storage)
        name = name.strip("/")

        while "//" in name:
            name = name.replace("//", "/")

        full_path = posixpath.join(self.group_index, name)

        if tensor_exists(full_path, self.storage, self.version_state["commit_id"]):
            raise TensorAlreadyExistsError(name)

        if full_path in self._groups:
            raise TensorGroupAlreadyExistsError(name)

        if not name or name in dir(self):
            raise InvalidTensorNameError(name)

        if not self._is_root():
            return self.root.create_tensor(
                full_path, htype, dtype, sample_compression, chunk_compression, **kwargs
            )

        if "/" in name:
            self._create_group(posixpath.split(name)[0])

        # Seperate meta and info

        htype_config = HTYPE_CONFIGURATIONS[htype].copy()
        info_keys = htype_config.pop("_info", [])
        info_kwargs = {}
        meta_kwargs = {}
        for k, v in kwargs.items():
            if k in info_keys:
                info_kwargs[k] = v
            else:
                meta_kwargs[k] = v

        # Set defaults
        for k in info_keys:
            if k not in info_kwargs:
                info_kwargs[k] = htype_config[k]

        create_tensor(
            name,
            self.storage,
            htype=htype,
            dtype=dtype,
            sample_compression=sample_compression,
            chunk_compression=chunk_compression,
            version_state=self.version_state,
            **meta_kwargs,
        )
        self.version_state["meta"].tensors.append(name)
        ffw_dataset_meta(self.version_state["meta"])
        self.storage.maybe_flush()
        tensor = Tensor(name, self.storage, self.version_state)  # type: ignore

        self.version_state["full_tensors"][name] = tensor
        tensor.info.update(info_kwargs)
        return tensor

    @hub_reporter.record_call
    def create_tensor_like(self, name: str, source: "Tensor") -> "Tensor":
        """Copies the `source` tensor's meta information and creates a new tensor with it. No samples are copied, only the meta/info for the tensor is.

        Args:
            name (str): Name for the new tensor.
            source (Tensor): Tensor who's meta/info will be copied. May or may not be contained in the same dataset.

        Returns:
            Tensor: New Tensor object.
        """

        info = source.info.__getstate__().copy()
        meta = source.meta.__getstate__().copy()
        del meta["min_shape"]
        del meta["max_shape"]
        del meta["length"]
        del meta["version"]

        destination_tensor = self.create_tensor(
            name,
            **meta,
        )
        destination_tensor.info.update(info)

        return destination_tensor

    __getattr__ = __getitem__

    def __setattr__(self, name: str, value):
        if isinstance(value, (np.ndarray, np.generic)):
            raise TypeError(
                "Setting tensor attributes directly is not supported. To add a tensor, use the `create_tensor` method."
                + "To add data to a tensor, use the `append` and `extend` methods."
            )
        else:
            return super().__setattr__(name, value)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def _load_version_info(self):
        """Loads data from version_control_file otherwise assume it doesn't exist and load all empty"""
        branch = "main"
        version_state = {"branch": branch}
        try:
            version_info = pickle.loads(self.storage[get_version_control_info_key()])
            version_state["branch_commit_map"] = version_info["branch_commit_map"]
            version_state["commit_node_map"] = version_info["commit_node_map"]
            commit_id = version_state["branch_commit_map"][branch]
            version_state["commit_id"] = commit_id
            version_state["commit_node"] = version_state["commit_node_map"][commit_id]
        except Exception:
            version_state["branch_commit_map"] = {}
            version_state["commit_node_map"] = {}
            # used to identify that this is the first commit so its data will not be in similar directory structure to the rest
            commit_id = FIRST_COMMIT_ID
            commit_node = CommitNode(branch, commit_id)
            version_state["commit_id"] = commit_id
            version_state["commit_node"] = commit_node
            version_state["branch_commit_map"][branch] = commit_id
            version_state["commit_node_map"][commit_id] = commit_node
        version_state["full_tensors"] = {}  # keeps track of the full unindexed tensors
        self.version_state = version_state

    def commit(self, message: Optional[str] = None) -> None:
        """Stores a snapshot of the current state of the dataset.
        Note: Commiting from a non-head node in any branch, will lead to an auto checkout to a new branch.
        This same behaviour will happen if new samples are added or existing samples are updated from a non-head node.

        Args:
            message (str, optional): Used to describe the commit.

        Returns:
            str: the commit id of the stored commit that can be used to access the snapshot.
        """
        commit_id = self.version_state["commit_id"]
        commit(self.version_state, self.storage, message)

        # do not store commit message
        hub_reporter.feature_report(
            feature_name="commit",
            parameters={},
        )

        return commit_id

    def checkout(self, address: str, create: bool = False) -> str:
        """Checks out to a specific commit_id or branch. If create = True, creates a new branch with name as address.
        Note: Checkout from a head node in any branch that contains uncommitted data will lead to an auto commit before the checkout.

        Args:
            address (str): The commit_id or branch to checkout to.
            create (bool): If True, creates a new branch with name as address.

        Returns:
            str: The commit_id of the dataset after checkout.
        """
        checkout(self.version_state, self.storage, address, create)

        # do not store address
        hub_reporter.feature_report(
            feature_name="checkout",
            parameters={"Create": str(create)},
        )

        return self.version_state["commit_id"]

    def log(self):
        """Displays the details of all the past commits."""
        # TODO: use logger.info instead of prints
        commit_node = self.version_state["commit_node"]
        logger.info("---------------\nHub Version Log\n---------------\n")
        logger.info(f"Current Branch: {self.version_state['branch']}\n")
        while commit_node:
            if commit_node.commit_time is not None:
                logger.info(f"{commit_node}\n")
            commit_node = commit_node.parent

    def _populate_meta(self):
        """Populates the meta information for the dataset."""

        if dataset_exists(self.storage):
            if self.verbose:
                logger.info(f"{self.path} loaded successfully.")
            load_meta(self.storage, self.version_state)

        elif not self.storage.empty():
            # dataset does not exist, but the path was not empty
            raise PathNotEmptyException

        else:
            if self.read_only:
                # cannot create a new dataset when in read_only mode.
                raise CouldNotCreateNewDatasetException(self.path)
            meta_key = get_dataset_meta_key(self.version_state["commit_id"])
            self.version_state["meta"] = DatasetMeta()
            self.storage[meta_key] = self.version_state["meta"]
            self.flush()
            self._register_dataset()

    def _register_dataset(self):
        # overridden in HubCloudDataset

        pass

    @property
    def read_only(self):
        return self._read_only

    @read_only.setter
    def read_only(self, value: bool):
        if value:
            self.storage.enable_readonly()
        else:
            self.storage.disable_readonly()
        self._read_only = value

    @hub_reporter.record_call
    def pytorch(
        self,
        transform: Optional[Callable] = None,
        tensors: Optional[Sequence[str]] = None,
        num_workers: int = 1,
        batch_size: int = 1,
        drop_last: bool = False,
        collate_fn: Optional[Callable] = None,
        pin_memory: bool = False,
        shuffle: bool = False,
        buffer_size: int = 10 * 1000,
        use_local_cache: bool = False,
        use_progress_bar: bool = False,
    ):
        """Converts the dataset into a pytorch Dataloader.

        Note:
            Pytorch does not support uint16, uint32, uint64 dtypes. These are implicitly type casted to int32, int64 and int64 respectively.
            This spins up it's own workers to fetch data.

        Args:
            transform (Callable, optional) : Transformation function to be applied to each sample.
            tensors (List, optional): Optionally provide a list of tensor names in the ordering that your training script expects. For example, if you have a dataset that has "image" and "label" tensors, if `tensors=["image", "label"]`, your training script should expect each batch will be provided as a tuple of (image, label).
            num_workers (int): The number of workers to use for fetching data in parallel.
            batch_size (int): Number of samples per batch to load. Default value is 1.
            drop_last (bool): Set to True to drop the last incomplete batch, if the dataset size is not divisible by the batch size.
                If False and the size of dataset is not divisible by the batch size, then the last batch will be smaller. Default value is False.
                Read torch.utils.data.DataLoader docs for more details.
            collate_fn (Callable, optional): merges a list of samples to form a mini-batch of Tensor(s). Used when using batched loading from a map-style dataset.
                Read torch.utils.data.DataLoader docs for more details.
            pin_memory (bool): If True, the data loader will copy Tensors into CUDA pinned memory before returning them. Default value is False.
                Read torch.utils.data.DataLoader docs for more details.
            shuffle (bool): If True, the data loader will shuffle the data indices. Default value is False.
            buffer_size (int): The size of the buffer used to prefetch/shuffle in MB. The buffer uses shared memory under the hood. Default value is 10 GB. Increasing the buffer_size will increase the extent of shuffling.
            use_local_cache (bool): If True, the data loader will use a local cache to store data. This is useful when the dataset can fit on the machine and we don't want to fetch the data multiple times for each iteration. Default value is False.
            use_progress_bar (bool): If True, tqdm will be wrapped around the returned dataloader. Default value is True.

        Returns:
            A torch.utils.data.DataLoader object.
        """
        from hub.integrations import dataset_to_pytorch

        dataloader = dataset_to_pytorch(
            self,
            transform,
            tensors,
            num_workers=num_workers,
            batch_size=batch_size,
            drop_last=drop_last,
            collate_fn=collate_fn,
            pin_memory=pin_memory,
            shuffle=shuffle,
            buffer_size=buffer_size,
            use_local_cache=use_local_cache,
        )

        if use_progress_bar:
            dataloader = tqdm(dataloader, desc=self.path, total=len(self) // batch_size)

        return dataloader

    def _get_total_meta(self):
        """Returns tensor metas all together"""
        return {
            tensor_key: tensor_value.meta
            for tensor_key, tensor_value in self.version_state["full_tensors"].items()
        }

    def _set_derived_attributes(self):
        """Sets derived attributes during init and unpickling."""

        if self.index.is_trivial() and self._is_root():
            self.storage.autoflush = True

        if not self.version_state:
            self._load_version_info()

        self._populate_meta()  # TODO: use the same scheme as `load_info`
        self.info = load_info(get_dataset_info_key(self.version_state["commit_id"]), self.storage, self.version_state)  # type: ignore
        self.index.validate(self.num_samples)

    @hub_reporter.record_call
    def tensorflow(self):
        """Converts the dataset into a tensorflow compatible format.

        See:
            https://www.tensorflow.org/api_docs/python/tf/data/Dataset

        Returns:
            tf.data.Dataset object that can be used for tensorflow training.
        """
        return dataset_to_tensorflow(self)

    def flush(self):
        """Necessary operation after writes if caches are being used.
        Writes all the dirty data from the cache layers (if any) to the underlying storage.
        Here dirty data corresponds to data that has been changed/assigned and but hasn't yet been sent to the
        underlying storage.
        """
        self.storage.flush()

    def clear_cache(self):
        """Flushes (see Dataset.flush documentation) the contents of the cache layers (if any) and then deletes contents
         of all the layers of it.
        This doesn't delete data from the actual storage.
        This is useful if you have multiple datasets with memory caches open, taking up too much RAM.
        Also useful when local cache is no longer needed for certain datasets and is taking up storage space.
        """
        if hasattr(self.storage, "clear_cache"):
            self.storage.clear_cache()

    def size_approx(self):
        """Estimates the size in bytes of the dataset.
        Includes only content, so will generally return an under-estimate.
        """
        tensors = self.version_state["full_tensors"].values()
        chunk_engines = [tensor.chunk_engine for tensor in tensors]
        size = sum(c.num_chunks * c.min_chunk_size for c in chunk_engines)
        return size

    @hub_reporter.record_call
    def delete(self, large_ok=False):
        """Deletes the entire dataset from the cache layers (if any) and the underlying storage.
        This is an IRREVERSIBLE operation. Data once deleted can not be recovered.

        Args:
            large_ok (bool): Delete datasets larger than 1GB. Disabled by default.
        """

        if not large_ok:
            size = self.size_approx()
            if size > hub.constants.DELETE_SAFETY_SIZE:
                logger.info(
                    f"Hub Dataset {self.path} was too large to delete. Try again with large_ok=True."
                )
                return

        unlock(self.storage)
        self.storage.clear()

    def __str__(self):
        path_str = ""
        if self.path:
            path_str = f"path='{self.path}', "

        mode_str = ""
        if self.read_only:
            mode_str = f"read_only=True, "

        index_str = f"index={self.index}, "
        if self.index.is_trivial():
            index_str = ""

        group_index_str = (
            f"group_index='{self.group_index}', " if self.group_index else ""
        )

        return f"Dataset({path_str}{mode_str}{index_str}{group_index_str}tensors={self.version_state['meta'].tensors})"

    __repr__ = __str__

    def _get_tensor_from_root(self, name: str) -> Optional[Tensor]:
        """Gets a tensor from the root dataset.
        Acesses storage only for the first call.
        """
        ret = self.version_state["full_tensors"].get(name)
        if ret is None:
            load_meta(self.storage, self.version_state)
            ret = self.version_state["full_tensors"].get(name)
        return ret

    def _has_group_in_root(self, name: str) -> bool:
        """Checks if a group exists in the root dataset.
        This is faster than checking `if group in self._groups:`
        """
        if name in self.version_state["meta"].groups:
            return True
        load_meta(self.storage, self.version_state)
        return name in self.version_state["meta"].groups

    @property
    def token(self):
        """Get attached token of the dataset"""

        return self._token

    @property
    def _ungrouped_tensors(self) -> Dict[str, Tensor]:
        """Top level tensors in this group that do not belong to any sub groups"""
        return {
            posixpath.basename(k): v
            for k, v in self.version_state["full_tensors"].items()
            if posixpath.dirname(k) == self.group_index
        }

    @property
    def _all_tensors_filtered(self) -> List[str]:
        """Names of all tensors belonging to this group, including those within sub groups"""
        load_meta(self.storage, self.version_state)
        return [
            posixpath.relpath(t, self.group_index)
            for t in self.version_state["full_tensors"]
            if not self.group_index or t.startswith(self.group_index + "/")
        ]

    @property
    def tensors(self) -> Dict[str, Tensor]:
        """All tensors belonging to this group, including those within sub groups. Always returns the sliced tensors."""
        return {
            t: self.version_state["full_tensors"][posixpath.join(self.group_index, t)][
                self.index
            ]
            for t in self._all_tensors_filtered
        }

    @property
    def _groups(self) -> List[str]:
        """Names of all groups in the root dataset"""
        meta_key = get_dataset_meta_key(self.version_state["commit_id"])
        return self.storage.get_cachable(meta_key, DatasetMeta).groups  # type: ignore

    @property
    def _groups_filtered(self) -> List[str]:
        """Names of all sub groups in this group"""
        groups_filtered = []
        for g in self._groups:
            dirname, basename = posixpath.split(g)
            if dirname == self.group_index:
                groups_filtered.append(basename)
        return groups_filtered

    @property
    def groups(self) -> Dict[str, "Dataset"]:
        """All sub groups in this group"""
        return {g: self[g] for g in self._groups_filtered}

    @property
    def commit_id(self) -> str:
        """The current commit_id of the dataset."""
        return self.version_state["commit_id"]

    @property
    def branch(self) -> str:
        """The current branch of the dataset"""
        return self.version_state["branch"]

    def _is_root(self) -> bool:
        return not self.group_index

    @property
    def parent(self):
        """Returns the parent of this group. Returns None if this is the root dataset"""
        if self._is_root():
            return None
        autoflush = self.storage.autoflush
        ds = self.__class__(
            self.storage,
            self.index,
            posixpath.dirname(self.group_index),
            self.read_only,
            self.public,
            self._token,
            self.verbose,
            path=self.path,
        )
        self.storage.autoflush = autoflush
        return ds

    @property
    def root(self):
        if self._is_root():
            return self
        autoflush = self.storage.autoflush
        ds = self.__class__(
            self.storage,
            self.index,
            "",
            self.read_only,
            self.public,
            self._token,
            self.verbose,
            path=self.path,
        )
        self.storage.autoflush = autoflush
        return ds

    def _create_group(self, name: str) -> "Dataset":
        """Internal method used by `create_group` and `create_tensor`."""
        meta_key = get_dataset_meta_key(self.version_state["commit_id"])
        meta = self.storage.get_cachable(meta_key, DatasetMeta)
        groups = meta.groups
        if not name or name in dir(self):
            raise InvalidTensorGroupNameError(name)
        fullname = name
        while name:
            if name in self.version_state["full_tensors"]:
                raise TensorAlreadyExistsError(name)
            groups.append(name)
            name, _ = posixpath.split(name)
        meta.groups = list(set(groups))
        self.storage[meta_key] = meta
        self.storage.maybe_flush()
        return self[fullname]

    def create_group(self, name: str) -> "Dataset":
        """Creates a tensor group. Intermediate groups in the path are also created."""
        if not self._is_root():
            return self.root.create_group(posixpath.join(self.group_index, name))
        name = name.strip("/")
        while "//" in name:
            name = name.replace("//", "/")
        if name in self._groups:
            raise TensorGroupAlreadyExistsError(name)
        return self._create_group(name)

    # the below methods are used by cloudpickle dumps
    def __origin__(self):
        return None

    def __values__(self):
        return None

    def __type__(self):
        return None

    def __union_params__(self):
        return None

    def __tuple_params__(self):
        return None

    def __result__(self):
        return None

    def __args__(self):
        return None
