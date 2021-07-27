from collections import namedtuple
import logging
import os
import sys
import traceback
import threading
import time
import traceback
from typing import Dict, List, Any, Type, Union, Sequence

import numpy as np
from numpy.core.fromnumeric import trace
import ray

from typing import Dict, List, Any, Union, Sequence, Tuple
from concurrent.futures import ThreadPoolExecutor
from readerwriterlock import rwlock

from malib import settings
from malib.backend.datapool.data_array import NumpyDataArray
from malib.utils.errors import OversampleError, NoEnoughDataError
from malib.utils.typing import BufferDescription, PolicyID, AgentID, Status
from malib.utils.logger import get_logger, Log
from malib.utils.logger import get_logger
from malib.utils.typing import BufferDescription, PolicyID, AgentID

import threading
import pickle as pkl


def _gen_table_name(env_id, main_id, pid):
    if isinstance(main_id, List):
        main_id = "_".join(sorted(main_id))
    if isinstance(pid, List):
        pid = "_".join(sorted(pid))
    return f"{env_id}_{main_id}_{pid}"


DATASET_TABLE_NAME_GEN = _gen_table_name
Batch = namedtuple("Batch", "identity, data")


class EpisodeLock:
    def __init__(self):
        self._pull_lock = 0
        self._push_lock = 0
        self._state = 1  # 1 for rollout, 0 for train

    @property
    def size(self):
        return 0

    def pull_and_push(self):
        return (self._pull_lock, self._push_lock)

    @property
    def lock(self):
        return self

    @property
    def push_lock_status(self):
        return self._push_lock

    @property
    def pull_lock_status(self):
        return self._pull_lock

    def lock_pull(self):
        if self._push_lock > 0:
            return Status.FAILED
        else:
            if self._state == 1:
                return Status.FAILED
            else:
                self._pull_lock += 1
        return Status.SUCCESS

    def lock_push(self):
        if self._pull_lock > 0:
            return Status.FAILED
        else:
            if self._state == 0:
                return Status.FAILED
            else:
                self._push_lock = self._push_lock + 1
        return Status.SUCCESS

    def unlock_pull(self):
        if self._pull_lock < 1:
            return Status.FAILED
        else:
            # self._pull_lock -= 1
            self._pull_lock = 0
            # FIXME(ziyu): check ?
            if self._pull_lock == 0:
                self._state = 1
        return Status.SUCCESS

    def unlock_push(self):
        if self._push_lock < 1:
            return Status.FAILED
        else:
            self._push_lock -= 1
            if self._push_lock == 0:
                self._state = 0
        return Status.SUCCESS


class Episode:
    """ Unlimited buffer """

    CUR_OBS = "obs"
    NEXT_OBS = "new_obs"
    ACTION = "action"
    ACTION_MASK = "action_mask"
    REWARD = "reward"
    DONE = "done"
    ACTION_DIST = "action_prob"
    # XXX(ming): seems useless
    INFO = "infos"

    # optional
    STATE_VALUE = "state_value_estimation"
    STATE_ACTION_VALUE = "state_action_value_estimation"
    CUR_STATE = "cur_state"  # current global state
    NEXT_STATE = "next_state"  # next global state
    LAST_REWARD = "last_reward"

    def __init__(
        self,
        env_id: str,
        policy_id: Union[PolicyID, Dict],
        capacity: int = None,
        other_columns: List[str] = None,
    ):
        """Create an episode instance

        :param str env_id: Environment id
        :param PolicyID policy_id: Policy id
        :param int capacity: Capacity
        :param List[str] other_columns: Extra columns you wanna collect
        """

        self.env_id = env_id
        self.policy_id = policy_id
        self._columns = [
            Episode.CUR_OBS,
            Episode.ACTION,
            Episode.NEXT_OBS,
            Episode.DONE,
            Episode.REWARD,
            Episode.ACTION_DIST,
        ]
        if other_columns:
            self._other_columns = other_columns
        else:
            self._other_columns = []

        assert isinstance(self._other_columns, List), self._other_columns

        self._size = 0
        self._capacity = capacity or settings.DEFAULT_EPISODE_CAPACITY
        self._data = None

        if capacity is not None:
            self._data = {
                col: NumpyDataArray(name=str(col), capacity=capacity)
                for col in self.columns
            }
        else:
            self._data = {
                col: NumpyDataArray(
                    name=str(col), init_capacity=settings.DEFAULT_EPISODE_INIT_CAPACITY
                )
                for col in self.columns
            }

    def reset(self, **kwargs):
        self.policy_id = kwargs.get("policy_id", self.policy_id)
        self._size = 0
        self._data = {
            col: NumpyDataArray(name=str(col), capacity=self.capacity)
            for col in self.columns
        }

    def empty(self) -> bool:
        return self._size == 0

    @property
    def capacity(self):
        return self._capacity

    @property
    def nbytes(self) -> int:
        return sum([e.nbytes for e in self._data.values()])

    @property
    def data(self):
        return self._data

    @property
    def columns(self):
        return self._columns + self._other_columns

    @property
    def other_columns(self):
        return self._other_columns

    @property
    def size(self):
        return self._size

    @property
    def data_bytes(self):
        return sum([col.nbytes for key, col in self._data.items()])

    def fill(self, **kwargs):
        for column in self.columns:
            self._data[column].fill(kwargs[column])
        self._size = len(self._data[Episode.CUR_OBS])
        self._capacity = max(self._size, self._capacity)

    def insert(self, **kwargs):
        # for column in self.columns:
        #     assert self._size == len(self._data[column]), (
        #         self._size,
        #         {c: len(self._data[c]) for c in self.columns},
        #     )
        for column in self.columns:
            if isinstance(kwargs[column], NumpyDataArray):
                assert kwargs[column]._data is not None, f"{column} has empty data"
                self._data[column].insert(kwargs[column].get_data())
            else:
                self._data[column].insert(kwargs[column])
        self._size = len(self._data[Episode.CUR_OBS])

    def sample(self, idxes=None, size=None) -> Any:
        assert idxes is None or size is None
        size = size or len(idxes)

        if self.size < size:
            raise OversampleError(f"batch size={size} data size={self.size}")

        if idxes is not None:
            return {k: self._data[k][idxes] for k in self.columns}

        if size is not None:
            indices = np.random.choice(self._size, size)
            return {k: self._data[k][indices] for k in self.columns}

    @classmethod
    def from_episode(cls, episode, capacity=None, fix_class=None):
        """Create an empty episode like episode with given capacity"""

        other_columns = episode.other_columns
        episode_class = fix_class or cls
        return episode_class(
            episode.env_id,
            episode.policy_id,
            capacity or episode.capacity,
            other_columns=other_columns,
        )

    @staticmethod
    def concatenate(*episodes, capacity=None):
        episodes = [e for e in episodes if e is not None]
        columns = episodes[0].columns
        other_columns = episodes[0].other_columns
        policy_id = episodes[0].policy_id
        env_id = episodes[0].env_id
        ans = Episode(env_id, policy_id, capacity=capacity, other_columns=other_columns)
        for e in episodes:
            data = {col: e.data[col].get_data() for col in columns}
            ans.insert(**data)
        return ans

    def format_to_dataset(self) -> List[Dict[str, Any]]:
        raise NotImplementedError


class SequentialEpisode(Episode):
    def __init__(
        self,
        env_id: str,
        policy_id: Union[PolicyID, Dict],
        capacity: int,
        other_columns: List[str],
    ):
        """Sequential episode is designed for sequential rollout. Different from `Episode`, it allows partially insertion, but require
        data clean mannually.

        Examples:
            >>> ep = SequentialEpisode(...)
            >>> ep.insert(**{Episode.OBS: .., Episode.ACTION: ...})
            >>> ep.insert(**{Episode.ACTION_MASK: ..., Episode.NEXT_OBS: ...})
            >>> # before send to offline dataset server or sampling, you need to do data alighment via executing `clean_data`
            >>> ep.clean_data()
            >>> # send to dataset server
            >>> server.save.remote(ep)
            >>> # or sampling
            >>> ep.sample(size=64)
            >>> # ...
        """
        super(SequentialEpisode, self).__init__(
            env_id, policy_id, capacity=capacity, other_columns=other_columns
        )
        self._cleaned = False

    def insert(self, **kwargs):
        self._cleaned = False
        for column, value in kwargs.items():
            assert column in self.columns, f"unregistered column: {column}"
            if isinstance(value, NumpyDataArray):
                self._data[column].insert(value.get_data())
            else:
                self._data[column].insert(value)

    def sample(self, idxes, size) -> Any:
        assert self._cleaned, "Data alignment is required before sampling!"
        return super(SequentialEpisode, self).sample(idxes=idxes, size=size)

    def clean_data(self):
        # check length
        self._data[Episode.NEXT_OBS].insert(self._data[Episode.CUR_OBS].get_data())
        self._data[Episode.NEXT_OBS].roll(-1)
        self._data[Episode.REWARD].roll(-1)
        self._data[Episode.DONE].roll(-1)


class MultiAgentEpisode(Episode):
    def __init__(
        self,
        env_id: str,
        agent_policy_mapping: Dict[AgentID, PolicyID],
        capacity: int = None,
        other_columns: Sequence[str] = None,
    ):
        super(MultiAgentEpisode, self).__init__(
            env_id, agent_policy_mapping, capacity, other_columns
        )
        self._data: Dict[AgentID, Episode] = {
            agent: Episode(pid, env_id, capacity, other_columns)
            for agent, pid in agent_policy_mapping.items()
        }

    @property
    def data(self):
        return {agent: episode.data for agent, episode in self._data.items()}

    @property
    def episodes(self):
        return self._data

    def fill(self, **kwargs):
        """ Format: {agent: {column: np.array, ...}, ...} """

        pre_size = list(kwargs.values())[0].size
        for agent, episode in kwargs.items():
            assert (
                episode.size == pre_size
            ), f"Inconsistency of episode size: {agent} {pre_size}/{episode.size}"

        _sizes = set()
        for agent, episode in self._data.items():
            episode.fill(**kwargs[agent])
            _sizes.add(episode.size)
        assert len(_sizes) == 1, f"Multiple size is not allowed: {_sizes}"
        self._size = _sizes.pop()

    def insert(self, **kwargs):
        """ Format: {agent: {column: np.array, ...}, ...} """
        _selected = list(kwargs.values())[0]
        if isinstance(_selected, Episode):
            _size = _selected.size
        elif isinstance(_selected, Dict):
            _size = len(list(_selected.values())[0])
        else:
            raise TypeError(f"Unexpected type: {type(_selected)}")
        for agent, episode in self._data.items():
            if isinstance(kwargs[agent], Episode):
                assert (
                    _size == kwargs[agent].size
                ), f"Inconsistency of inserted episodes, expect {_size} while actual {episode.size}"
                episode.insert(**kwargs[agent].data)
            else:
                assert _size == len(list(kwargs[agent].values())[0])
                episode.insert(**kwargs[agent])
            self._size = episode.size

    def sample(self, idxes=None, size=None):
        return {
            agent: episode.sample(idxes, size) for agent, episode in self._data.items()
        }

    @classmethod
    def from_data(cls, env_id, policy_id_mapping, data):
        columns = list(list(data.values())[0].keys())
        episode = cls(env_id, policy_id_mapping)
        episode._columns = columns
        episode._data = data
        episode._size = len(list(list(data.values())[0].values())[0])
        return episode

    @classmethod
    def from_episodes(cls, env_id, policy_id_mapping, episodes: Dict[str, Episode]):
        columns = list(episodes.values())[0].columns
        episode = cls(env_id, policy_id_mapping)
        episode._columns = columns
        episode._data = episodes
        episode._size = list(episodes.values())[0].size
        return episode

    @staticmethod
    def concatenate(*multiagent_episodes, capacity=None):
        # FIXME(ming): check columns equivalence
        if multiagent_episodes[0] is None:
            multiagent_episodes = multiagent_episodes[1:]
        policy_ids = multiagent_episodes[0].policy_id
        env_id = multiagent_episodes[0].env_id

        episodes = {}
        # concatenate by agent wise
        for agent in multiagent_episodes[0].episodes.keys():
            episodes[agent] = Episode.concatenate(
                *[me.episodes[agent] for me in multiagent_episodes], capacity=capacity
            )

        return MultiAgentEpisode.from_episodes(env_id, policy_ids, episodes)

    def format_to_dataset(self) -> List[Dict[str, Any]]:
        raise NotImplementedError


class Table:
    def __init__(self, name, multi_agent: bool = False):
        """One table for one episode."""

        self._name = name
        self._lock_status: EpisodeLock = EpisodeLock()
        self._threading_lock = threading.Lock()
        self._rwlock = rwlock.RWLockFairD()
        self._episode: Union[Episode, MultiAgentEpisode] = None
        self._is_multi_agent = multi_agent

    @property
    def name(self):
        return self._name

    @property
    def is_multi_agent(self) -> bool:
        return self._is_multi_agent

    @property
    def episode(self) -> Union[Episode, MultiAgentEpisode]:
        return self._episode

    @property
    def size(self):
        with self._rwlock.gen_rlock():
            return self._episode.size if self._episode is not None else 0

    @property
    def capacity(self):
        with self._rwlock.gen_rlock():
            return self._episode.capacity

    def set_episode(
        self, episode: Dict[AgentID, Union[Episode, SequentialEpisode]], capacity: int
    ):
        """If the current table has no episode, inititalize one for it."""

        with self._rwlock.gen_wlock():
            assert self._episode is None
            _episode = list(episode.values())[0]
            if self._is_multi_agent:
                self._episode = MultiAgentEpisode(
                    env_id=_episode.env_id,
                    agent_policy_mapping={
                        aid: e.policy_id for aid, e in episode.items()
                    },
                    capacity=capacity,
                    other_columns=_episode.other_columns,
                )
            else:
                self._episode = Episode(
                    env_id=_episode.env_id,
                    policy_id=_episode.policy_id,
                    capacity=capacity,
                    other_columns=_episode.other_columns,
                )

    @staticmethod
    def gen_table_name(*args, **kwargs):
        return DATASET_TABLE_NAME_GEN(*args, **kwargs)

    def fill(self, **kwargs):
        with self._rwlock.gen_wlock():
            self._episode.fill(**kwargs)

    def insert(self, **kwargs):
        try:
            with self._rwlock.gen_wlock():
                if not self._is_multi_agent:
                    assert len(kwargs) == 1, kwargs
                    kwargs = list(kwargs.values())[0].data
                # print("ready to insert")
                self._episode.insert(**kwargs)
                # print(f"after inserted: {self._episode.size}")
        except Exception as e:
            print(traceback.format_exc())

    def sample(self, idxes=None, size=None) -> Tuple[Any, str]:
        with self._rwlock.gen_rlock():
            data = self._episode.sample(idxes, size)
        return data

    def lock_push_pull(self, lock_type):
        with self._threading_lock:
            if lock_type == "push":
                # lock for push
                status = self._lock_status.lock_push()
            else:
                # lock for pull
                if self._episode is None:
                    status = Status.FAILED
                else:
                    status = self._lock_status.lock_pull()
        return status

    def unlock_push_pull(self, lock_type):
        with self._threading_lock:
            if lock_type == "push":
                status = self._lock_status.unlock_push()
            else:
                status = self._lock_status.unlock_pull()
        return status

    @property
    def lock(self) -> EpisodeLock:
        with self._threading_lock:
            return self._lock_status

    def reset(self, **kwargs):
        with self._rwlock.gen_wlock():
            self._episode.reset(**kwargs)


@ray.remote
class OfflineDataset:
    def __init__(self, dataset_config: Dict[str, Any], exp_cfg: Dict[str, Any]):
        self._episode_capacity = dataset_config.get(
            "episode_capacity", settings.DEFAULT_EPISODE_CAPACITY
        )
        self._learning_start = dataset_config.get("learning_start", 64)
        self._tables: Dict[str, Table] = dict()
        self._threading_lock = threading.Lock()
        self._threading_pool = ThreadPoolExecutor()
        self.logger = get_logger(
            log_level=settings.LOG_LEVEL,
            log_dir=settings.LOG_DIR,
            name="offline_dataset",
            remote=settings.USE_REMOTE_LOGGER,
            mongo=settings.USE_MONGO_LOGGER,
            **exp_cfg,
        )

    def lock(self, lock_type: str, desc: Dict[AgentID, Any]):
        status = dict.fromkeys(desc, Status.FAILED)
        for agent, item in desc.items():
            table_name = Table.gen_table_name(
                env_id=item.env_id,
                main_id=agent,
                pid=item.policy_id,
            )
            self.check_table(table_name, desc[agent])
            table = self._tables[table_name]
            status[agent] = table.lock_push_pull(lock_type)
        return status

    def unlock(self, lock_type: str, desc: Dict[AgentID, BufferDescription]):
        status = dict.fromkeys(desc, Status.SUCCESS)
        for agent, item in desc.items():
            table_name = Table.gen_table_name(
                env_id=item.env_id,
                main_id=agent,
                pid=item.policy_id,
            )
            self.check_table(table_name, desc[agent])
            table = self._tables[table_name]
            table.unlock_push_pull(lock_type)

        return status

    def check_table(
        self, table_name: str, episode: Dict[AgentID, Union[SequentialEpisode, Episode]]
    ):
        """Check table existing, if not, create a new table. If `episode` is not None and table has no episode yet, it
        will be used to create an empty episode with default capacity for table.

        :param str table_name: Registered table name, to index table.
        :param Episode episode: Episode to insert. Default to None.
        """
        with self._threading_lock:
            if self._tables.get(table_name, None) is None:
                self._tables[table_name] = Table(
                    table_name, multi_agent=len(episode) > 1
                )
                self._tables[table_name].set_episode(
                    episode, capacity=self._episode_capacity
                )

    def save(self, agent_episodes: Dict[AgentID, Episode], wait_for_ready: bool = True):
        """Accept a dictionary of agent episodes, save them to a named table. If there is only one agent episode
        in the dict, we use `Episode`, otherwise `MultiAgentEpisode` will be used.
        """

        insert_results = []
        episode = list(agent_episodes.values())[0]
        main_ids = list(agent_episodes.keys())
        table_name = Table.gen_table_name(
            env_id=episode.env_id,
            main_id=main_ids,
            pid=[agent_episodes[aid].policy_id for aid in main_ids],
        )
        self.check_table(table_name, agent_episodes)
        insert_results.append(
            self._threading_pool.submit(
                self._tables[table_name].insert, **agent_episodes
            )
        )
        self.logger.debug(f"Threads created for insertion on table={table_name}")

        if wait_for_ready:
            for fut in insert_results:
                while not fut.done():
                    pass

    @Log.method_timer(enable=settings.PROFILING)
    def load_from_dataset(
        self,
        file: str,
        env_id: str,
        policy_id: PolicyID,
        agent_id: AgentID,
    ):
        """
        Expect the dataset to be in the form of List[ Dict[str, Any] ]
        """

        # FIXME(ming): check its functionality
        with open(file, "rb") as f:
            dataset = pkl.load(file=f)
            keys = set()
            for batch in dataset:
                keys = keys.union(batch.keys())

            table_size = len(dataset)
            table_name = DATASET_TABLE_NAME_GEN(
                env_id=env_id,
                main_id=agent_id,
                pid=policy_id,
            )
            if self._tables.get(table_name, None) is None:
                self._tables[table_name] = Episode(
                    env_id, policy_id, other_columns=None
                )

            for batch in dataset:
                assert isinstance(batch, Dict)
                self._tables[table_name].insert(**batch)

            self.logger.debug(
                f"table={table_name} capacity={self._tables[table_name].capacity} size={self._tables[table_name].size}"
            )

    @Log.method_timer(enable=settings.PROFILING)
    def load(self, file) -> List[Dict[str, str]]:
        # FIXME(ming): check its functionality
        with open(file, "rb") as f:
            self._tables = pkl.load(f)
            table_size = len(self._tables)
            table_descs: List[Dict[str, str]] = [None] * table_size

            idx = 0
            for table_name, table in self._tables.items():
                table_descs[idx] = {
                    "name": table.name,
                    "env_id": table.env_id,
                    "policy_id": table.policy_id,
                    "agent_id": table.agent_id,
                    "capacity": table.capacity,
                    "columns": table.columns,
                }
                idx += 1

            return table_descs

    @Log.method_timer(enable=settings.PROFILING)
    def dump(self, file, protocol=None, *args, **kwargs):
        protocol = protocol or settings.PICKLE_PROTOCOL_VER
        with open(file, "wb") as f:
            pkl.dump(self._tables, file=f, protocol=protocol, *args, **kwargs)

    @Log.method_timer(enable=settings.PROFILING)
    def dump_to_dataset(self, protocol=None, root=None):
        protocol = protocol or settings.PICKLE_PROTOCOL_VER
        output_dir = os.path.join(root or settings.DATASET_DIR, str(time.time()))
        try:
            os.makedirs(output_dir)
        except Exception as e:
            pass

        for table_name, table in self._tables.items():
            dataset_path = os.path.join(output_dir, f"episode_{id(table)}")
            # FIXME(ming): no such an interface - format_to_dataset
            dataset = table.format_to_dataset()
            with open(dataset_path + ".pkl", "wb") as f:
                pkl.dump(obj=dataset, file=f, protocol=protocol)

    # @Log.method_timer(enable=settings.PROFILING)
    def sample(self, buffer_desc: BufferDescription) -> Tuple[Batch, str]:
        """Sample data from the top for training, default is random sample from sub batches.

        :param BufferDesc buffer_desc: Description of sampling a buffer.
            used to index the buffer slot
        :return: a tuple of samples and information
        """

        # generate idxes from idxes manager
        info = "OK"
        try:
            res = {}
            # with Log.timer(log=settings.PROFILING, logger=self.logger):
            table_name = Table.gen_table_name(
                env_id=buffer_desc.env_id,
                main_id=buffer_desc.agent_id,
                pid=buffer_desc.policy_id,
            )
            table = self._tables[table_name]
            res = table.sample(size=buffer_desc.batch_size)
            res = Batch(identity=buffer_desc.agent_id, data=res)
        except KeyError as e:
            info = f"data table `{table_name}` has not been created {list(self._tables.keys())}"
            res = Batch(identity=buffer_desc.agent_id, data=None)
        except OversampleError as e:
            info = f"No enough data: table_size={table.size} batch_size={buffer_desc.batch_size} table_name={table_name}"
            res = Batch(identity=buffer_desc.agent_id, data=None)
        except Exception as e:
            print(traceback.format_exc())
            res = Batch(identity=buffer_desc.agent_id, data=None)
            info = "others"
        return res, info

    def shutdown(self):
        self.logger.info("Server terminated.")
