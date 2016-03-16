import asyncio
import logging

from kafka.common import OffsetAndMetadata, TopicPartition
from kafka.coordinator.assignors.roundrobin import RoundRobinPartitionAssignor
from kafka.consumer.subscription_state import SubscriptionState

from aiokafka.client import AIOKafkaClient
from aiokafka.group_coordinator import GroupCoordinator
from aiokafka.fetcher import Fetcher
from aiokafka import __version__

log = logging.getLogger(__name__)


class AIOKafkaConsumer(object):
    """Consume records from a Kafka cluster.

    The consumer will transparently handle the failure of servers in the Kafka
    cluster, and adapt as topic-partitions are created or migrate between
    brokers. It also interacts with the assigned kafka Group Coordinator node
    to allow multiple consumers to load balance consumption of topics (requires
    kafka >= 0.9.0.0).

    Arguments:
        *topics (str): optional list of topics to subscribe to. If not set,
            call subscribe() or assign() before consuming records.
        bootstrap_servers: 'host[:port]' string (or list of 'host[:port]'
            strings) that the consumer should contact to bootstrap initial
            cluster metadata. This does not have to be the full node list.
            It just needs to have at least one broker that will respond to a
            Metadata API Request. Default port is 9092. If no servers are
            specified, will default to localhost:9092.
        client_id (str): a name for this client. This string is passed in
            each request to servers and can be used to identify specific
            server-side log entries that correspond to this client. Also
            submitted to GroupCoordinator for logging with respect to
            consumer group administration. Default: 'aiokafka-{version}'
        group_id (str or None): name of the consumer group to join for dynamic
            partition assignment (if enabled), and to use for fetching and
            committing offsets. If None, auto-partition assignment (via
            group coordinator) and offset commits are disabled.
            Default: None
        key_deserializer (callable): Any callable that takes a
            raw message key and returns a deserialized key.
        value_deserializer (callable, optional): Any callable that takes a
            raw message value and returns a deserialized value.
        fetch_min_bytes (int): Minimum amount of data the server should
            return for a fetch request, otherwise wait up to
            fetch_max_wait_ms for more data to accumulate. Default: 1.
        fetch_max_wait_ms (int): The maximum amount of time in milliseconds
            the server will block before answering the fetch request if
            there isn't sufficient data to immediately satisfy the
            requirement given by fetch_min_bytes. Default: 500.
        max_partition_fetch_bytes (int): The maximum amount of data
            per-partition the server will return. The maximum total memory
            used for a request = #partitions * max_partition_fetch_bytes.
            This size must be at least as large as the maximum message size
            the server allows or else it is possible for the producer to
            send messages larger than the consumer can fetch. If that
            happens, the consumer can get stuck trying to fetch a large
            message on a certain partition. Default: 1048576.
        request_timeout_ms (int): Client request timeout in milliseconds.
            Default: 40000.
        retry_backoff_ms (int): Milliseconds to backoff when retrying on
            errors. Default: 100.
        reconnect_backoff_ms (int): The amount of time in milliseconds to
            wait before attempting to reconnect to a given host.
            Default: 50.
        auto_offset_reset (str): A policy for resetting offsets on
            OffsetOutOfRange errors: 'earliest' will move to the oldest
            available message, 'latest' will move to the most recent. Any
            ofther value will raise the exception. Default: 'latest'.
        enable_auto_commit (bool): If true the consumer's offset will be
            periodically committed in the background. Default: True.
        auto_commit_interval_ms (int): milliseconds between automatic
            offset commits, if enable_auto_commit is True. Default: 5000.
        check_crcs (bool): Automatically check the CRC32 of the records
            consumed. This ensures no on-the-wire or on-disk corruption to
            the messages occurred. This check adds some overhead, so it may
            be disabled in cases seeking extreme performance. Default: True
        metadata_max_age_ms (int): The period of time in milliseconds after
            which we force a refresh of metadata even if we haven't seen any
            partition leadership changes to proactively discover any new
            brokers or partitions. Default: 300000
        partition_assignment_strategy (list): List of objects to use to
            distribute partition ownership amongst consumer instances when
            group management is used. Default: [RoundRobinPartitionAssignor]
        heartbeat_interval_ms (int): The expected time in milliseconds
            between heartbeats to the consumer coordinator when using
            Kafka's group management feature. Heartbeats are used to ensure
            that the consumer's session stays active and to facilitate
            rebalancing when new consumers join or leave the group. The
            value must be set lower than session_timeout_ms, but typically
            should be set no higher than 1/3 of that value. It can be
            adjusted even lower to control the expected time for normal
            rebalances. Default: 3000
        session_timeout_ms (int): The timeout used to detect failures when
            using Kafka's group managementment facilities. Default: 30000
        consumer_timeout_ms (int): number of millisecond to poll available
            fetched messages. Default: 100
        api_version (str): specify which kafka API version to use.
            AIOKafkaConsumer supports Kafka API versions >=0.9 only.
            If set to 'auto', will attempt to infer the broker version by
            probing various APIs. Default: auto
    """
    def __init__(self, *topics, loop,
                 bootstrap_servers='localhost',
                 client_id='aiokafka-'+__version__,
                 group_id=None,
                 key_deserializer=None, value_deserializer=None,
                 fetch_max_wait_ms=500,
                 fetch_min_bytes=1,
                 max_partition_fetch_bytes=1 * 1024 * 1024,
                 request_timeout_ms=40 * 1000,
                 retry_backoff_ms=100,
                 reconnect_backoff_ms=50,
                 auto_offset_reset='latest',
                 enable_auto_commit=True,
                 auto_commit_interval_ms=5000,
                 check_crcs=True,
                 metadata_max_age_ms=5 * 60 * 1000,
                 partition_assignment_strategy=(RoundRobinPartitionAssignor,),
                 heartbeat_interval_ms=3000,
                 session_timeout_ms=30000,
                 consumer_timeout_ms=100,
                 api_version='auto'):
        if api_version not in ('auto', '0.9'):
            raise ValueError("Unsupported Kafka API version")
        self._client = AIOKafkaClient(
            loop=loop, bootstrap_servers=bootstrap_servers,
            client_id=client_id, metadata_max_age_ms=metadata_max_age_ms,
            request_timeout_ms=request_timeout_ms)

        self._api_version = api_version
        self._group_id = group_id
        self._heartbeat_interval_ms = heartbeat_interval_ms
        self._retry_backoff_ms = retry_backoff_ms
        self._enable_auto_commit = enable_auto_commit
        self._auto_commit_interval_ms = auto_commit_interval_ms
        self._partition_assignment_strategy = partition_assignment_strategy
        self._key_deserializer = key_deserializer
        self._value_deserializer = value_deserializer
        self._fetch_min_bytes = fetch_min_bytes
        self._fetch_max_wait_ms = fetch_max_wait_ms
        self._max_partition_fetch_bytes = max_partition_fetch_bytes
        self._consumer_timeout = consumer_timeout_ms / 1000.
        self._check_crcs = check_crcs
        self._subscription = SubscriptionState(auto_offset_reset)
        self._fetcher = None
        self._coordinator = None
        self._closed = False
        self._iterator = None
        self._loop = loop
        self._topics = topics

        if topics:
            self._client.set_topics(topics)
            self._subscription.subscribe(topics=topics)

    @asyncio.coroutine
    def start(self):
        yield from self._client.bootstrap()

        # Check Broker Version if not set explicitly
        if self._api_version == 'auto':
            self._api_version = yield from self._client.check_version()
        # Convert api_version config to tuple for easy comparisons
        self._api_version = tuple(
            map(int, self._api_version.split('.')))

        if self._api_version < (0, 9):
            raise ValueError(
                "Unsupported Kafka version: {}".format(self._api_version))

        self._fetcher = Fetcher(
            self._client, self._subscription, loop=self._loop,
            key_deserializer=self._key_deserializer,
            value_deserializer=self._value_deserializer,
            fetch_min_bytes=self._fetch_min_bytes,
            fetch_max_wait_ms=self._fetch_max_wait_ms,
            max_partition_fetch_bytes=self._max_partition_fetch_bytes,
            check_crcs=self._check_crcs,
            fetcher_timeout=self._consumer_timeout)

        self._coordinator = GroupCoordinator(
            self._client, self._subscription, loop=self._loop,
            group_id=self._group_id,
            heartbeat_interval_ms=self._heartbeat_interval_ms,
            retry_backoff_ms=self._retry_backoff_ms,
            enable_auto_commit=self._enable_auto_commit,
            auto_commit_interval_ms=self._auto_commit_interval_ms,
            assignors=self._partition_assignment_strategy)

        if self._group_id is not None:
            yield from self._coordinator.ensure_active_group()
        elif self._subscription.needs_partition_assignment:
            yield from self._client.force_metadata_update()
            partitions = []
            for topic in self._topics:
                p_ids = self.partitions_for_topic(topic)
                for p_id in p_ids:
                    partitions.append(TopicPartition(topic, p_id))
            self._subscription.unsubscribe()
            self._subscription.assign_from_user(partitions)

    def assign(self, partitions):
        """Manually assign a list of TopicPartitions to this consumer.

        Arguments:
            partitions (list of TopicPartition): assignment for this instance.

        Raises:
            IllegalStateError: if consumer has already called subscribe()

        Warning:
            It is not possible to use both manual partition assignment with
            assign() and group assignment with subscribe().

        Note:
            This interface does not support incremental assignment and will
            replace the previous assignment (if there was one).

        Note:
            Manual topic assignment through this method does not use the
            consumer's group management functionality. As such, there will be
            no rebalance operation triggered when group membership or cluster
            and topic metadata change.
        """
        self._subscription.assign_from_user(partitions)
        self._client.set_topics([tp.topic for tp in partitions])

    def assignment(self):
        """Get the TopicPartitions currently assigned to this consumer.

        If partitions were directly assigned using assign(), then this will
        simply return the same partitions that were previously assigned.
        If topics were subscribed using subscribe(), then this will give the
        set of topic partitions currently assigned to the consumer (which may
        be none if the assignment hasn't happened yet or if the partitions are
        in the process of being reassigned).

        Returns:
            set: {TopicPartition, ...}
        """
        return self._subscription.assigned_partitions()

    @asyncio.coroutine
    def stop(self):
        """Close the consumer, waiting indefinitely for any needed cleanup."""
        if self._closed:
            return
        log.debug("Closing the KafkaConsumer.")
        self._closed = True
        if self._coordinator:
            yield from self._coordinator.close()
        if self._fetcher:
            yield from self._fetcher.close()
        yield from self._client.close()
        log.debug("The KafkaConsumer has closed.")

    @asyncio.coroutine
    def commit(self, offsets=None):
        """Commit offsets to kafka, blocking until success or error

        This commits offsets only to Kafka.
        The offsets committed using this API will be used on the first fetch
        after every rebalance and also on startup. As such, if you needto store
        offsets in anything other than Kafka, this API should not be used.

        Blocks until either the commit succeeds or an unrecoverable error is
        encountered (in which case it is thrown to the caller).

        Currently only supports kafka-topic offset storage (not zookeeper)

        Arguments:
            offsets (dict, optional): {TopicPartition: OffsetAndMetadata} dict
                to commit with the configured group_id. Defaults to current
                consumed offsets for all subscribed partitions.
        """
        assert self._group_id is not None, 'Requires group_id'

        if offsets is None:
            offsets = self._subscription.all_consumed_offsets()
        else:
            # validate `offsets` structure
            assert all(map(lambda k: isinstance(k, TopicPartition), offsets))
            assert all(map(lambda v: isinstance(v, OffsetAndMetadata),
                           offsets.values()))
        yield from self._coordinator.commit_offsets(offsets)

    @asyncio.coroutine
    def committed(self, partition):
        """Get the last committed offset for the given partition

        This offset will be used as the position for the consumer
        in the event of a failure.

        This call may block to do a remote call if the partition in question
        isn't assigned to this consumer or if the consumer hasn't yet
        initialized its cache of committed offsets.

        Arguments:
            partition (TopicPartition): the partition to check

        Returns:
            The last committed offset, or None if there was no prior commit.
        """
        assert self._group_id is not None, 'Requires group_id'
        if self._subscription.is_assigned(partition):
            committed = self._subscription.assignment[partition].committed
            if committed is None:
                yield from self._coordinator.refresh_committed_offsets()
                committed = self._subscription.assignment[partition].committed
        else:
            commit_map = yield from self._coordinator.fetch_committed_offsets(
                [partition])
            if partition in commit_map:
                committed = commit_map[partition].offset
            else:
                committed = None
        return committed

    @asyncio.coroutine
    def topics(self):
        """Get all topics the user is authorized to view.

        Returns:
            set: topics
        """
        cluster = yield from self._client.fetch_all_metadata()
        return cluster.topics()

    def partitions_for_topic(self, topic):
        """Get metadata about the partitions for a given topic.

        Arguments:
            topic (str): topic to check

        Returns:
            set: partition ids
        """
        return self._client.cluster.partitions_for_topic(topic)

    @asyncio.coroutine
    def position(self, partition):
        """Get the offset of the next record that will be fetched

        Arguments:
            partition (TopicPartition): partition to check

        Returns:
            int: offset
        """
        assert self._subscription.is_assigned(partition), \
            'Partition is not assigned'
        offset = self._subscription.assignment[partition].position
        if offset is None:
            yield from self._update_fetch_positions(partition)
            offset = self._subscription.assignment[partition].position
        return offset

    def highwater(self, partition):
        """Last known highwater offset for a partition

        A highwater offset is the offset that will be assigned to the next
        message that is produced. It may be useful for calculating lag, by
        comparing with the reported position. Note that both position and
        highwater refer to the *next* offset -- i.e., highwater offset is
        one greater than the newest availabel message.

        Highwater offsets are returned in FetchResponse messages, so will
        not be available if not FetchRequests have been sent for this partition
        yet.

        Arguments:
            partition (TopicPartition): partition to check

        Returns:
            int or None: offset if available
        """
        assert self._subscription.is_assigned(partition), \
            'Partition is not assigned'
        return self._subscription.assignment[partition].highwater

    def seek(self, partition, offset):
        """Manually specify the fetch offset for a TopicPartition.

        Overrides the fetch offsets that the consumer will use on the next
        poll(). If this API is invoked for the same partition more than once,
        the latest offset will be used on the next poll(). Note that you may
        lose data if this API is arbitrarily used in the middle of consumption,
        to reset the fetch offsets.

        Arguments:
            partition (TopicPartition): partition for seek operation
            offset (int): message offset in partition

        Raises:
            AssertionError: if offset is not an int >= 0;
                            or if partition is not currently assigned.
        """
        assert isinstance(offset, int) and offset >= 0, 'Offset must be >= 0'
        assert partition in self._subscription.assigned_partitions(), \
            'Unassigned partition'
        log.debug("Seeking to offset %s for partition %s", offset, partition)
        self._subscription.assignment[partition].seek(offset)

    @asyncio.coroutine
    def seek_to_committed(self, *partitions):
        """Seek to the committed offset for partitions
        Arguments:
            *partitions: optionally provide specific TopicPartitions,
                otherwise default to all assigned partitions

        Raises:
            AssertionError: if any partition is not currently assigned, or if
                no partitions are assigned
        """
        if not partitions:
            partitions = self._subscription.assigned_partitions()
            assert partitions, 'No partitions are currently assigned'
        else:
            for p in partitions:
                assert p in self._subscription.assigned_partitions(), \
                    'Unassigned partition'

        for tp in partitions:
            log.debug("Seeking to committed of partition %s", tp)
            offset = yield from self.committed(tp)
            if offset and offset > 0:
                self.seek(tp, offset)

    def subscribe(self, topics=(), pattern=None, listener=None):
        """Subscribe to a list of topics, or a topic regex pattern

        Partitions will be dynamically assigned via a group coordinator.
        Topic subscriptions are not incremental: this list will replace the
        current assignment (if there is one).

        This method is incompatible with assign()

        Arguments:
           topics (list): List of topics for subscription.
           pattern (str): Pattern to match available topics. You must provide
               either topics or pattern, but not both.
           listener (ConsumerRebalanceListener): Optionally include listener
               callback, which will be called before and after each rebalance
               operation.
               As part of group management, the consumer will keep track of
               the list of consumers that belong to a particular group and
               will trigger a rebalance operation if one of the following
               events trigger:

               * Number of partitions change for any of the subscribed topics
               * Topic is created or deleted
               * An existing member of the consumer group dies
               * A new member is added to the consumer group

               When any of these events are triggered, the provided listener
               will be invoked first to indicate that the consumer's
               assignment has been revoked, and then again when the new
               assignment has been received. Note that this listener will
               immediately override any listener set in a previous call
               to subscribe. It is guaranteed, however, that the partitions
               revoked/assigned
               through this interface are from topics subscribed in this call.
        Raises:
            IllegalStateError: if called after previously calling assign()
            AssertionError: if neither topics or pattern is provided
            TypeError: if listener is not a ConsumerRebalanceListener
        """
        # SubscriptionState handles error checking
        self._subscription.subscribe(topics=topics,
                                     pattern=pattern,
                                     listener=listener)

        # regex will need all topic metadata
        if pattern is not None:
            self._client.set_topics([])
            log.debug("Subscribed to topic pattern: %s", pattern)
        else:
            self._client.set_topics(self._subscription.group_subscription())
            log.debug("Subscribed to topic(s): %s", topics)

    def subscription(self):
        """Get the current topic subscription.

        Returns:
            set: {topic, ...}
        """
        return self._subscription.subscription

    def unsubscribe(self):
        """Unsubscribe from all topics and clear all assigned partitions."""
        self._subscription.unsubscribe()
        self._client.set_topics([])
        log.debug(
            "Unsubscribed all topics or patterns and assigned partitions")

    @asyncio.coroutine
    def _update_fetch_positions(self, partitions):
        """
        Set the fetch position to the committed position (if there is one)
        or reset it using the offset reset policy the user has configured.

        Arguments:
            partitions (List[TopicPartition]): The partitions that need
                updating fetch positions

        Raises:
            NoOffsetForPartitionError: If no offset is stored for a given
                partition and no offset reset policy is defined
        """
        if self._group_id is not None:
            # refresh commits for all assigned partitions
            yield from self._coordinator.refresh_committed_offsets()

        # then do any offset lookups in case some positions are not known
        yield from self._fetcher.update_fetch_positions(partitions)

    @asyncio.coroutine
    def getone(self, *partitions):
        """
        Get one message from Kafka
        If no new messages occured, this method will wait it

        Arguments:
            partitions (List[TopicPartition]): The partitions that need
                fetching message. If no one partition specified then all
                subscribed partitions will be used

        Returns:
            instance of collections.namedtuple("ConsumerRecord",
                ["topic", "partition", "offset", "key", "value"])
        """
        assert all(map(lambda k: isinstance(k, TopicPartition), partitions))

        while True:
            while not self._subscription.assigned_partitions():
                # no one partition for fetch... waiting consumers rebalance
                yield from asyncio.sleep(
                    self._consumer_timeout, loop=self._loop)

            # fetch positions if we have partitions we're subscribed
            # to that we don't know the offset for
            if not self._subscription.has_all_fetch_positions():
                yield from self._update_fetch_positions(
                    self._subscription.missing_fetch_positions())

            msg = self._fetcher.next_record(partitions)
            if msg is None:
                yield from asyncio.sleep(
                    self._consumer_timeout, loop=self._loop)
                continue

            return msg

    @asyncio.coroutine
    def getmany(self, *partitions, timeout_ms=0):
        """Fetch data from assigned topics / partitions.

        Records are fetched and returned in batches by topic-partition.
        On each poll, consumer will try to use the last consumed offset as the
        starting offset and fetch sequentially. The last consumed offset can be
        manually set through seek(partition, offset) or automatically set as
        the last committed offset for the subscribed list of partitions.
        Incompatible with iterator interface (get_message) -- use one or the
        other, not both.

        Arguments:
            partitions (List[TopicPartition]): The partitions that need
                fetching message. If no one partition specified then all
                subscribed partitions will be used
            timeout_ms (int, optional): milliseconds spent waiting in poll if
                data is not available in the buffer. If 0, returns immediately
                with any records that are available currently in the buffer,
                else returns empty. Must not be negative. Default: 0
        Returns:
            dict: topic to list of records since the last fetch for the
                subscribed list of topics and partitions
        """
        assert all(map(lambda k: isinstance(k, TopicPartition), partitions))

        # fetch positions if we have partitions we're subscribed
        # to that we don't know the offset for
        if not self._subscription.has_all_fetch_positions():
            yield from self._update_fetch_positions(
                self._subscription.missing_fetch_positions())

        timeout = timeout_ms / 1000.
        start_time = self._loop.time()
        while True:
            records = self._fetcher.fetched_records(partitions)
            if records:
                return records

            yield from asyncio.sleep(self._consumer_timeout, loop=self._loop)

            if (self._loop.time() - start_time) >= timeout:
                # timeouted, no new fetched data now
                return {}
