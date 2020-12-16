from __future__ import annotations
import logging
from datetime import datetime, timedelta

from abc import ABC, abstractmethod

from django.core.management.base import BaseCommand
from django.db import connection
from django.db.backends.utils import CursorWrapper
from django.db.models import Count
from django.db.utils import OperationalError
from typing import List

from treeherder.config import settings
from treeherder.model.models import Job, JobGroup, JobType, Machine, Repository
from treeherder.perf.exceptions import MaxRuntimeExceeded, NoDataCyclingAtAll
from treeherder.perf.models import PerformanceDatum, PerformanceSignature, PerformanceAlertSummary
from treeherder.services.taskcluster import TaskclusterModel, DEFAULT_ROOT_URL as root_url
from treeherder.perf.data_cycling.signature_remover import PublicSignatureRemover
from treeherder.perf.data_cycling.max_runtime import MaxRuntime

logging.basicConfig(format='%(levelname)s:%(message)s')

TREEHERDER = 'treeherder'
PERFHERDER = 'perfherder'
TREEHERDER_SUBCOMMAND = 'from:treeherder'
PERFHERDER_SUBCOMMAND = 'from:perfherder'
MINIMUM_PERFHERDER_EXPIRE_INTERVAL = 365

logger = logging.getLogger(__name__)


def has_valid_explicit_days(func):
    def wrapper(*args, **kwargs):
        days = kwargs.get('days')
        if (days is not None) and settings.SITE_HOSTNAME != 'treeherder-prototype2.herokuapp.com':
            raise ValueError(
                'Cannot override perf data retention parameters on projects other than treeherder-prototype2'
            )
        func(*args, **kwargs)

    return wrapper


class DataCycler(ABC):
    def __init__(
        self, chunk_size: int, sleep_time: int, is_debug: bool = None, days: int = None, **kwargs
    ):
        self.chunk_size = chunk_size
        self.sleep_time = sleep_time
        self.is_debug = is_debug or False

    @abstractmethod
    def cycle(self):
        pass


class TreeherderCycler(DataCycler):
    DEFAULT_CYCLE_INTERVAL = 120  # in days

    def __init__(
        self, chunk_size: int, sleep_time: int, is_debug: bool = None, days: int = None, **kwargs
    ):
        super().__init__(chunk_size, sleep_time, is_debug, **kwargs)
        self.days = days or self.DEFAULT_CYCLE_INTERVAL
        self.cycle_interval = timedelta(days=self.days)

    def cycle(self):
        logger.warning(
            f"Cycling {TREEHERDER.title()} data older than {self.days} days...\n"
            f"Cycling jobs across all repositories"
        )

        try:
            rs_deleted = Job.objects.cycle_data(
                self.cycle_interval, self.chunk_size, self.sleep_time
            )
            logger.warning("Deleted {} jobs".format(rs_deleted))
        except OperationalError as e:
            logger.error("Error running cycle_data: {}".format(e))

        self._remove_leftovers()

    def _remove_leftovers(self):
        logger.warning('Pruning ancillary data: job types, groups and machines')

        def prune(id_name, model):
            logger.warning('Pruning {}s'.format(model.__name__))
            used_ids = Job.objects.only(id_name).values_list(id_name, flat=True).distinct()
            unused_ids = model.objects.exclude(id__in=used_ids).values_list('id', flat=True)

            logger.warning('Removing {} records from {}'.format(len(unused_ids), model.__name__))

            while len(unused_ids):
                delete_ids = unused_ids[: self.chunk_size]
                logger.warning('deleting {} of {}'.format(len(delete_ids), len(unused_ids)))
                model.objects.filter(id__in=delete_ids).delete()
                unused_ids = unused_ids[self.chunk_size :]

        prune('job_type_id', JobType)
        prune('job_group_id', JobGroup)
        prune('machine_id', Machine)


class PerfherderCycler(DataCycler):
    DEFAULT_MAX_RUNTIME = timedelta(hours=23)

    @has_valid_explicit_days
    def __init__(
        self,
        chunk_size: int,
        sleep_time: int,
        is_debug: bool = None,
        days: int = None,
        strategies: List[RemovalStrategy] = None,
        **kwargs,
    ):
        super().__init__(chunk_size, sleep_time, is_debug)
        self.strategies = strategies or RemovalStrategy.fabricate_all_strategies(
            chunk_size, days=days
        )
        self.timer = MaxRuntime()

    @property
    def max_timestamp(self):
        """
        Returns the most recent timestamp from all strategies.
        """
        strategy = max(self.strategies, key=lambda s: s.max_timestamp)
        return strategy.max_timestamp

    def cycle(self):
        """
        Delete data older than cycle_interval, splitting the target data
        into chunks of chunk_size size.
        """
        logger.warning(f"Cycling {PERFHERDER.title()} data...")
        self.timer.start_timer()

        try:
            for strategy in self.strategies:
                try:
                    logger.warning(f'Cycling data using {strategy.name}...')
                    self._delete_in_chunks(strategy)
                except NoDataCyclingAtAll as ex:
                    logger.warning(str(ex))

            self._remove_leftovers()
        except MaxRuntimeExceeded as ex:
            logger.warning(ex)

    def _remove_leftovers(self):
        # remove any signatures which are
        # no longer associated with a job
        signatures = PerformanceSignature.objects.filter(last_updated__lte=self.max_timestamp)
        tc_model = TaskclusterModel(
            root_url, client_id=settings.NOTIFY_CLIENT_ID, access_token=settings.NOTIFY_ACCESS_TOKEN
        )
        signatures_remover = PublicSignatureRemover(timer=self.timer, taskcluster_model=tc_model)
        signatures_remover.remove_in_chunks(signatures)

        # remove empty alert summaries
        logger.warning('Removing alert summaries which no longer have any alerts...')
        (
            PerformanceAlertSummary.objects.prefetch_related('alerts', 'related_alerts')
            .annotate(
                total_alerts=Count('alerts'),
                total_related_alerts=Count('related_alerts'),
            )
            .filter(
                total_alerts=0,
                total_related_alerts=0,
                # WARNING! Don't change this without proper approval!           #
                # Otherwise we risk deleting data that's actively investigated  #
                # and cripple the perf sheriffing process!                      #
                created__lt=(datetime.now() - timedelta(days=180)),
                #################################################################
            )
            .delete()
        )

    def _delete_in_chunks(self, strategy: RemovalStrategy):
        any_successful_attempt = False

        with connection.cursor() as cursor:
            while True:
                self.timer.quit_on_timeout()

                try:
                    strategy.remove(using=cursor)
                except Exception as ex:
                    self.__handle_chunk_removal_exception(ex, cursor, any_successful_attempt)
                    break
                else:
                    deleted_rows = cursor.rowcount

                    if deleted_rows == 0 or deleted_rows == -1:
                        break  # either finished removing all expired data or failed
                    else:
                        any_successful_attempt = True
                        logger.debug(
                            'Successfully deleted {} performance datum rows'.format(deleted_rows)
                        )

    def __handle_chunk_removal_exception(
        self, exception, cursor: CursorWrapper, any_successful_attempt: bool
    ):
        msg = 'Failed to delete performance data chunk'
        if hasattr(cursor, '_last_executed'):
            msg = f'{msg}, while running "{cursor._last_executed}" query'

        if any_successful_attempt:
            # an intermittent error may have occurred
            logger.warning(f'{msg}: (Exception: {exception})')
        else:
            logger.warning(msg)
            raise NoDataCyclingAtAll() from exception


class RemovalStrategy(ABC):
    @property
    @abstractmethod
    def CYCLE_INTERVAL(self) -> int:
        """
        expressed in days
        """
        pass

    @has_valid_explicit_days
    def __init__(self, chunk_size: int, days: int = None):
        days = days or self.CYCLE_INTERVAL

        self._cycle_interval = timedelta(days=days)
        self._chunk_size = chunk_size
        self._max_timestamp = datetime.now() - self._cycle_interval

    @abstractmethod
    def remove(self, using: CursorWrapper):
        pass

    @property
    @abstractmethod
    def max_timestamp(self) -> datetime:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @staticmethod
    def fabricate_all_strategies(*args, **kwargs) -> List[RemovalStrategy]:
        return [
            MainRemovalStrategy(*args, **kwargs),
            TryDataRemoval(*args, **kwargs),
            IrrelevantDataRemoval(*args, **kwargs),
            StalledDataRemoval(*args, **kwargs),
            # append here any new strategies
            # ...
        ]


class MainRemovalStrategy(RemovalStrategy):
    """
    Removes `performance_datum` rows
    that are at least 1 year old.
    """

    @property
    def CYCLE_INTERVAL(self) -> int:
        # WARNING!! Don't override this without proper approval!
        return 365  # days                                     #
        ########################################################

    def __init__(self, chunk_size: int, days: int = None):
        super().__init__(chunk_size, days=days)
        self._manager = PerformanceDatum.objects

    @property
    def max_timestamp(self):
        return self._max_timestamp

    def remove(self, using: CursorWrapper):
        chunk_size = self._find_ideal_chunk_size()
        using.execute(
            '''
            DELETE FROM `performance_datum`
            WHERE push_timestamp <= %s
            LIMIT %s
        ''',
            [self._max_timestamp, chunk_size],
        )

    @property
    def name(self) -> str:
        return 'main removal strategy'

    def _find_ideal_chunk_size(self) -> int:
        max_id = self._manager.filter(push_timestamp__gt=self._max_timestamp).order_by('-id')[0].id
        older_ids = self._manager.filter(
            push_timestamp__lte=self._max_timestamp, id__lte=max_id
        ).order_by('id')[: self._chunk_size]

        return len(older_ids) or self._chunk_size


class TryDataRemoval(RemovalStrategy):
    """
    Removes `performance_datum` rows
    that originate from `try` repository and
    that are more than 6 weeks old.
    """

    SIGNATURE_BULK_SIZE = 10

    @property
    def CYCLE_INTERVAL(self) -> int:
        # WARNING!! Don't override this without proper approval!
        return 42  # days                                      #
        ########################################################

    def __init__(self, chunk_size: int, days: int = None):
        super().__init__(chunk_size, days=days)

        self.__try_repo_id = None
        self.__target_signatures = None
        self.__try_signatures = None

    @property
    def max_timestamp(self):
        return self._max_timestamp

    @property
    def try_repo(self):
        if self.__try_repo_id is None:
            self.__try_repo_id = Repository.objects.get(name='try').id
        return self.__try_repo_id

    @property
    def target_signatures(self):
        if self.__target_signatures is None:
            self.__target_signatures = self.try_signatures[: self.SIGNATURE_BULK_SIZE]
            if len(self.__target_signatures) == 0:
                msg = 'No try signatures found.'
                logger.warning(msg)  # no try data is not normal
                raise LookupError(msg)
        return self.__target_signatures

    @property
    def try_signatures(self):
        if self.__try_signatures is None:
            self.__try_signatures = list(
                PerformanceSignature.objects.filter(repository=self.try_repo)
                .order_by('-id')
                .values_list('id', flat=True)
            )
        return self.__try_signatures

    def remove(self, using: CursorWrapper):
        """
        @type using: database connection cursor
        """

        while True:
            try:
                self.__attempt_remove(using)

                deleted_rows = using.rowcount
                if deleted_rows > 0:
                    break  # deletion was successful

                self.__lookup_new_signature()  # to remove data from
            except LookupError as ex:
                logger.debug(f'Could not target any (new) try signature to delete data from. {ex}')
                break

    @property
    def name(self) -> str:
        return 'try data removal strategy'

    def __attempt_remove(self, using):
        total_signatures = len(self.target_signatures)
        from_target_signatures = ' OR '.join(['signature_id =  %s'] * total_signatures)

        delete_try_data = f'''
            DELETE FROM `performance_datum`
            WHERE repository_id = %s AND push_timestamp <= %s AND ({from_target_signatures})
            LIMIT %s
        '''

        using.execute(
            delete_try_data,
            [self.try_repo, self._max_timestamp, *self.target_signatures, self._chunk_size],
        )

    def __lookup_new_signature(self):
        self.__target_signatures = self.__try_signatures[: self.SIGNATURE_BULK_SIZE]
        del self.__try_signatures[: self.SIGNATURE_BULK_SIZE]

        if len(self.__target_signatures) == 0:
            raise LookupError('Exhausted all signatures originating from try repository.')


class IrrelevantDataRemoval(RemovalStrategy):
    """
    Removes `performance_datum` rows that originate
    from repositories, other than the ones mentioned
    in `RELEVANT_REPO_NAMES`, that are more than 6 months old.
    """

    RELEVANT_REPO_NAMES = [
        'autoland',
        'mozilla-central',
        'mozilla-beta',
        'fenix',
        'reference-browser',
    ]

    @property
    def CYCLE_INTERVAL(self) -> int:
        # WARNING!! Don't override this without proper approval!
        return 180  # days                                     #
        ########################################################

    def __init__(self, chunk_size: int, days: int = None):
        super().__init__(chunk_size, days=days)

        self._manager = PerformanceDatum.objects
        self.__relevant_repos = None

    @property
    def max_timestamp(self):
        return self._max_timestamp

    @property
    def relevant_repositories(self):
        if self.__relevant_repos is None:
            self.__relevant_repos = list(
                Repository.objects.filter(name__in=self.RELEVANT_REPO_NAMES).values_list(
                    'id', flat=True
                )
            )
            if len(self.__relevant_repos) != len(self.RELEVANT_REPO_NAMES):
                logger.warning("Failed to find all relevant repositories in the database")

        return self.__relevant_repos

    @property
    def name(self) -> str:
        return 'irrelevant data removal strategy'

    def remove(self, using: CursorWrapper):
        chunk_size = self._find_ideal_chunk_size()

        using.execute(
            '''
                DELETE FROM `performance_datum`
                WHERE (repository_id NOT IN %s) AND push_timestamp <= %s
                LIMIT %s
            ''',
            [
                tuple(self.relevant_repositories),
                self._max_timestamp,
                chunk_size,
            ],
        )

    def _find_ideal_chunk_size(self) -> int:
        max_id_of_non_expired_row = (
            self._manager.filter(push_timestamp__gt=self._max_timestamp)
            .exclude(repository_id__in=self.relevant_repositories)
            .order_by('-id')[0]
            .id
        )
        older_perf_data_rows = (
            self._manager.filter(
                push_timestamp__lte=self._max_timestamp, id__lte=max_id_of_non_expired_row
            )
            .exclude(repository_id__in=self.relevant_repositories)
            .order_by('id')[: self._chunk_size]
        )
        return len(older_perf_data_rows) or self._chunk_size


class StalledDataRemoval(RemovalStrategy):
    """
    Removes `performance_datum` rows from `performance_signature`s
    that haven't been updated in the last 4 months.
    """

    @property
    def CYCLE_INTERVAL(self) -> int:
        # WARNING!! Don't override this without proper approval!
        return 120  # days                                     #
        ########################################################

    def __init__(self, chunk_size: int, days: int = None):
        super().__init__(chunk_size, days=days)

        self._target_signature = None
        self._removable_signatures = None

    @property
    def target_signature(self) -> PerformanceSignature:
        try:
            if self._target_signature is None:
                self._target_signature = self.removable_signatures.pop()
        except IndexError:
            msg = 'No stalled signature found.'
            logger.warning(msg)  # no stalled data is not normal
            raise LookupError(msg)
        return self._target_signature

    @property
    def removable_signatures(self) -> List[PerformanceSignature]:
        if self._removable_signatures is None:
            self._removable_signatures = list(
                PerformanceSignature.objects.filter(last_updated__lte=self._max_timestamp).order_by(
                    'last_updated'
                )
            )
        return self._removable_signatures

    def remove(self, using: CursorWrapper):
        while True:
            try:
                self.__attempt_remove(using)

                deleted_rows = using.rowcount
                if deleted_rows > 0:
                    break  # deletion was successful

                self.__lookup_new_signature()  # to remove data from
            except LookupError as ex:
                logger.debug(
                    f'Could not target any (new) stalled signature to delete data from. {ex}'
                )
                break

    @property
    def max_timestamp(self) -> datetime:
        return self._max_timestamp

    @property
    def name(self) -> str:
        return 'stalled data removal strategy'

    def __attempt_remove(self, using: CursorWrapper):
        using.execute(
            '''
                DELETE FROM `performance_datum`
                WHERE repository_id = %s AND signature_id = %s AND push_timestamp <= %s
                LIMIT %s
            ''',
            [
                self.target_signature.repository_id,
                self.target_signature.id,
                self._max_timestamp,
                self._chunk_size,
            ],
        )

    def __lookup_new_signature(self):
        try:
            self._target_signature = self._removable_signatures.pop()
        except IndexError:
            raise LookupError('Exhausted all stalled signatures.')


class Command(BaseCommand):
    help = """Cycle data that exceeds the time constraint limit"""
    CYCLER_CLASSES = {
        TREEHERDER: TreeherderCycler,
        PERFHERDER: PerfherderCycler,
    }

    def add_arguments(self, parser):
        parser.add_argument(
            '--debug',
            action='store_true',
            dest='is_debug',
            default=False,
            help='Write debug messages to stdout',
        )
        parser.add_argument(
            '--days',
            action='store',
            dest='days',
            type=int,
            help=(
                "Data cycle interval expressed in days. "
                "On Perfherder specifically, this only applies for `treeherder-prototype2` "
                "environment; supplying it for other environments is illegal."
            ),
        )
        parser.add_argument(
            '--chunk-size',
            action='store',
            dest='chunk_size',
            default=100,
            type=int,
            help=(
                'Define the size of the chunks ' 'Split the job deletes into chunks of this size'
            ),
        )
        parser.add_argument(
            '--sleep-time',
            action='store',
            dest='sleep_time',
            default=0,
            type=int,
            help='How many seconds to pause between each query. Ignored when cycling performance data.',
        )
        subparsers = parser.add_subparsers(
            description='Data producers from which to expire data', dest='data_source'
        )
        subparsers.add_parser(TREEHERDER_SUBCOMMAND)  # default subcommand even if not provided

        # Perfherder will have its own specifics
        subparsers.add_parser(PERFHERDER_SUBCOMMAND)

    def handle(self, *args, **options):
        data_cycler = self.fabricate_data_cycler(options)
        data_cycler.cycle()

    def fabricate_data_cycler(self, options):
        data_source = options.pop('data_source') or TREEHERDER_SUBCOMMAND
        data_source = data_source.split(':')[1]

        cls = self.CYCLER_CLASSES[data_source]
        return cls(**options)
