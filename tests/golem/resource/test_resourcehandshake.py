from golem_messages import message
import os
import types
from unittest.mock import Mock, patch, ANY
import uuid

from golem.model import Database
from golem.network.hyperdrive.client import HyperdriveClientOptions, \
    HyperdriveClient
from golem.resource.base.resourcesmanager import ResourceStorage
from golem.resource.dirmanager import DirManager
from golem.resource.hyperdrive.resourcesmanager import HyperdriveResourceManager
from golem.resource.resourcehandshake import ResourceHandshake, \
    ResourceHandshakeSessionMixin
from golem.testutils import TempDirFixture


def mock_async_run(async_request, success, error):
    m, a, k = async_request.method, async_request.args, async_request.kwargs

    try:
        result = m(*a, **k)
    except Exception as exc:
        error(exc)
    else:
        success(result)


class TestResourceHandshake(TempDirFixture):

    def setUp(self):
        super().setUp()
        key_id = str(uuid.uuid4())
        self.handshake = ResourceHandshake(key_id)

    def test_start(self):
        handshake = self.handshake
        assert not handshake.started

        handshake.start(self.tempdir)
        assert os.path.exists(handshake.file)
        assert handshake.nonce == handshake.read_nonce(handshake.file)

    def test_status(self):
        handshake = self.handshake

        assert not handshake.success()
        assert not handshake.finished()

        handshake.start(self.tempdir)

        assert not handshake.success()
        assert not handshake.finished()

        assert handshake.local_result is None
        assert not handshake.verify_local('invalid nonce')
        assert handshake.local_result is False

        assert handshake.verify_local(handshake.nonce)
        assert handshake.local_result is True
        assert handshake.remote_result is None
        assert not handshake.success()
        assert not handshake.finished()

        handshake.remote_verdict(False)

        assert handshake.remote_result is False
        assert not handshake.success()
        assert handshake.finished()

        handshake.remote_verdict(True)

        assert handshake.remote_result is True
        assert handshake.success()
        assert handshake.finished()


@patch('golem.resource.resourcehandshake.async_run', side_effect=mock_async_run)
@patch('twisted.internet.reactor', create=True)
@patch('twisted.internet.task', create=True)
class TestResourceHandshakeSessionMixin(TempDirFixture):

    def setUp(self):
        super().setUp()

        self.key_id = str(uuid.uuid4())
        self.message = dict(
            node_name='test node',
            task_id=str(uuid.uuid4()),
            perf_index=4000,
            price=5,
            max_resource_size=10 * 10 ** 8,
            max_memory_size=10 * 10 ** 8,
            num_cores=10
        )

    def test_request_task_handshake(self, *_):
        session = MockTaskSession(self.tempdir)
        session._start_handshake = Mock()
        session.send = Mock()

        session.request_task(**self.message)
        assert session._start_handshake.called

    def test_request_task_success(self, *_):
        session = MockTaskSession(self.tempdir)
        session._handshake_required = Mock()
        session._start_handshake = Mock()
        session.send = Mock()

        session._handshake_required.return_value = False
        session.request_task(**self.message)

        assert not session.disconnect.called
        assert session.send.called

        slots = session.send.call_args[0][0].__slots__

        msg = message.WantToComputeTask(**self.message)
        msg_slots = msg.__slots__

        assert slots == msg_slots

    def test_request_task_failure(self, *_):
        session = MockTaskSession(self.tempdir)
        session._handshake_required = Mock()
        session._start_handshake = Mock()
        session._handshake_error = Mock()
        session.send = Mock()

        session._handshake_required.return_value = False
        session._block_peer(session.key_id)
        session.request_task(**self.message)

        assert not session._start_handshake.called
        assert session._handshake_error.called

    def test_react_to_resource_handshake_start(self, *_):
        session = MockTaskSession(self.tempdir)
        session._start_handshake = Mock()
        session._download_handshake_nonce = Mock()
        session._handshake_error = Mock()

        resource = str(uuid.uuid4())
        msg = message.ResourceHandshakeStart(resource)
        session._react_to_resource_handshake_start(msg)

        assert session._start_handshake.called
        assert not session._handshake_error.called
        session._download_handshake_nonce.assert_called_with(ANY, resource)

    def test_react_to_resource_handshake_start_blocked_peer(self, *_):
        session = MockTaskSession(self.tempdir)
        session._start_handshake = Mock()
        session._download_handshake_nonce = Mock()
        session._handshake_error = Mock()

        msg = message.ResourceHandshakeStart(str(uuid.uuid4()))
        session._block_peer(session.key_id)
        session._react_to_resource_handshake_start(msg)

        assert not session._download_handshake_nonce.called
        assert not session._start_handshake.called
        assert session._handshake_error.called

    def test_react_to_resource_handshake_start_upload_failure(self, *_):
        session = MockTaskSession(self.tempdir, successful_uploads=False)
        session._download_handshake_nonce = Mock()
        session._handshake_error = Mock()

        msg = message.ResourceHandshakeStart(str(uuid.uuid4()))

        session._react_to_resource_handshake_start(msg)

        assert session._handshake_error.called
        assert session._download_handshake_nonce.called

    def test_react_to_resource_handshake_start_download_failure(self, *_):
        session = MockTaskSession(self.tempdir, successful_downloads=False)
        session._start_handshake = Mock()
        session._handshake_error = Mock()

        msg = message.ResourceHandshakeStart(str(uuid.uuid4()))
        handshake = ResourceHandshake(self.key_id)

        session._set_handshake(session.key_id, handshake)
        session._react_to_resource_handshake_start(msg)

        assert session._handshake_error.called

    def test_react_to_resource_handshake_nonce(self, *_):
        session = MockTaskSession(self.tempdir)
        session._finalize_handshake = Mock()
        session._handshake_error = Mock()

        handshake = ResourceHandshake(self.key_id)
        handshake.start(self.tempdir)

        msg = message.ResourceHandshakeNonce(handshake.nonce)

        session._set_handshake(session.key_id, handshake)
        session._react_to_resource_handshake_nonce(msg)

        assert session._finalize_handshake.called
        assert not session._handshake_error.called

    def test_react_to_resource_handshake_nonce_failure(self, *_):
        session = MockTaskSession(self.tempdir)
        session._finalize_handshake = Mock()
        session._handshake_error = Mock()

        handshake = ResourceHandshake(self.key_id)
        handshake.start(self.tempdir)

        msg = message.ResourceHandshakeNonce(handshake.nonce)
        session._react_to_resource_handshake_nonce(msg)

        assert not session._finalize_handshake.called
        assert session._handshake_error.called

        session._set_handshake(session.key_id, handshake)
        msg = message.ResourceHandshakeNonce(str(uuid.uuid4()))
        session._react_to_resource_handshake_nonce(msg)

        assert not session._finalize_handshake.called
        assert session._handshake_error.called

    def test_react_to_resource_handshake_verdict(self, *_):
        session = MockTaskSession(self.tempdir, successful_uploads=False)
        session._finalize_handshake = Mock()
        session._handshake_error = Mock()

        handshake = ResourceHandshake(self.key_id)
        handshake.start(self.tempdir)

        msg = message.ResourceHandshakeVerdict(handshake.nonce, accepted=True)
        session._react_to_resource_handshake_nonce(msg)

        assert not session._finalize_handshake.called
        assert session._handshake_error.called

        session._set_handshake(session.key_id, handshake)
        msg = message.ResourceHandshakeVerdict(str(uuid.uuid4()), accepted=False)
        session._react_to_resource_handshake_nonce(msg)

        assert not session._finalize_handshake.called
        assert session._handshake_error.called

    def test_react_to_resource_handshake_verdict_failure(self, *_):
        session = MockTaskSession(self.tempdir, successful_uploads=False)
        session._finalize_handshake = Mock()
        session._handshake_error = Mock()

        handshake = ResourceHandshake(self.key_id)
        handshake.start(self.tempdir)

        msg = message.ResourceHandshakeVerdict(handshake.nonce, accepted=False)
        session._react_to_resource_handshake_nonce(msg)

        assert not session._finalize_handshake.called
        assert session._handshake_error.called

        session._set_handshake(session.key_id, handshake)
        msg = message.ResourceHandshakeVerdict(str(uuid.uuid4()), accepted=False)
        session._react_to_resource_handshake_nonce(msg)

        assert not session._finalize_handshake.called
        assert session._handshake_error.called

    def test_handshake_required(self, *_):
        session = MockTaskSession(self.tempdir)
        session._handshake_error = Mock()

        assert not session._handshake_required(None)
        assert session._handshake_error.called

        session._handshake_error.reset_mock()

        assert session._handshake_required(session.key_id)
        assert not session._handshake_in_progress(session.key_id)
        assert not session._handshake_error.called

        handshake = ResourceHandshake(self.key_id)
        session._set_handshake(session.key_id, handshake)

        assert not session._handshake_required(session.key_id)
        assert session._handshake_in_progress(session.key_id)
        assert not session._handshake_error.called

        handshake.local_result = True
        handshake.remote_result = True

        assert not session._handshake_required(session.key_id)
        assert not session._handshake_in_progress(session.key_id)
        assert not session._handshake_error.called

        session._remove_handshake(session.key_id)
        session._block_peer(session.key_id)

        assert not session._handshake_required(session.key_id)
        assert not session._handshake_in_progress(session.key_id)
        assert not session._handshake_error.called

    def test_handshake_in_progress(self, *_):
        session = MockTaskSession(self.tempdir)
        session._handshake_error = Mock()

        assert not session._handshake_in_progress(None)
        assert session._handshake_error.called

        session._handshake_error.reset_mock()

        assert not session._handshake_in_progress(session.key_id)
        assert not session._handshake_error.called

        handshake = ResourceHandshake(self.key_id)
        handshake.start(self.tempdir)
        session._set_handshake(session.key_id, handshake)

        assert session._handshake_in_progress(session.key_id)

        handshake.local_result = True
        handshake.remote_result = False

        assert not session._handshake_in_progress(session.key_id)

    def test_start_handshake(self, *_):
        session = MockTaskSession(self.tempdir)
        session._set_handshake = Mock()
        session._start_handshake_timer = Mock()
        session._share_handshake_nonce = Mock()

        def raise_exception(*_):
            raise RuntimeError('Test exception')

        with patch('golem.resource.resourcehandshake.ResourceHandshake.start',
                   side_effect=raise_exception):

            session._start_handshake(session.key_id)

            assert not session._set_handshake.called
            assert not session._start_handshake_timer.called
            assert not session._share_handshake_nonce.called

        session._start_handshake(session.key_id)

        assert session._set_handshake.called
        assert session._start_handshake_timer.called
        assert session._share_handshake_nonce.called

    def test_handshake_timer(self, task, *_):
        session = MockTaskSession(self.tempdir)

        session._start_handshake_timer()
        assert task.deferLater.called

    def test_finalize_handshake(self, *_):
        session = MockTaskSession(self.tempdir)

        session._finalize_handshake(session.key_id)
        assert not session.send.called

        handshake = ResourceHandshake(self.key_id)
        handshake.local_result = False
        handshake.remote_result = True
        session._set_handshake(session.key_id, handshake)

        session._finalize_handshake(session.key_id)
        assert not session.send.called

        handshake.message = self.message

        session._finalize_handshake(session.key_id)
        assert not session.send.called

        handshake.local_result = True
        handshake.remote_result = True

        session._finalize_handshake(session.key_id)
        assert session.send.called

    def test_handshake_error(self, *_):
        session = MockTaskSession(self.tempdir)
        session._block_peer = Mock()
        session._finalize_handshake = Mock()

        session._handshake_error(session.key_id, 'Test error')
        assert session._block_peer.called
        assert session._finalize_handshake.called
        assert session.task_server.task_computer.session_closed.called
        assert not session.disconnect.called

    def test_handshake_timeout(self, *_):
        session = MockTaskSession(self.tempdir)
        session._block_peer = Mock()
        session._finalize_handshake = Mock()

        session._handshake_timeout(session.key_id)
        assert not session._block_peer.called
        assert not session._finalize_handshake.called
        assert not session.task_server.task_computer.session_closed.called
        assert not session.dropped.called

        handshake = ResourceHandshake(self.key_id)
        handshake.local_result = False
        handshake.remote_result = True
        session._set_handshake(session.key_id, handshake)

        session._handshake_timeout(session.key_id)
        assert session._block_peer.called
        assert session._finalize_handshake.called
        assert session.task_server.task_computer.session_closed.called
        assert session.dropped.called

    def test_get_set_remove_handshake(self, *_):
        session = MockTaskSession(self.tempdir)
        handshake = ResourceHandshake(self.key_id)
        key_id = session.key_id

        assert not session._get_handshake(key_id)
        session._set_handshake(key_id, handshake)
        assert session._get_handshake(key_id)
        session._remove_handshake(key_id)
        assert not session._get_handshake(key_id)

    def test_block_peer(self, *_):
        session = MockTaskSession(self.tempdir)
        key_id = session.key_id

        assert not session._is_peer_blocked(key_id)
        session._block_peer(key_id)
        assert session._is_peer_blocked(key_id)


@patch('golem.resource.resourcehandshake.async_run', side_effect=mock_async_run)
@patch('twisted.internet.reactor', create=True)
@patch('twisted.internet.task', create=True)
class TestResourceHandshakeShare(TempDirFixture):

    def setUp(self):
        super().setUp()

        self.db = Database(self.tempdir)
        self.key_id = str(uuid.uuid4())

    def tearDown(self):
        self.db.close()

    def test_flow(self, *_):
        local_dir = os.path.join(self.tempdir, 'local')
        remote_dir = os.path.join(self.tempdir, 'remote')

        os.makedirs(local_dir, exist_ok=True)
        os.makedirs(remote_dir, exist_ok=True)

        local_session = MockTaskSession(local_dir)
        remote_session = MockTaskSession(remote_dir)

        local_session._handshake_error = Mock(
            side_effect=local_session._handshake_error)
        local_session._finalize_handshake = Mock()
        remote_session._handshake_error = Mock(
            side_effect=remote_session._handshake_error)
        remote_session._finalize_handshake = Mock()

        self.__create_task_server(local_session)
        self.__create_task_server(remote_session)

        local_session.key_id = remote_session.task_server.node.key
        remote_session.key_id = local_session.task_server.node.key

        # Start handshake

        local_session._start_handshake(local_session.key_id)
        msg = local_session.send.call_args[0][0]
        local_hash = msg.resource

        remote_session._start_handshake(remote_session.key_id)
        msg = remote_session.send.call_args[0][0]
        remote_hash = msg.resource

        local_session.send.reset_mock()
        remote_session.send.reset_mock()

        # Download nonces on both sides

        local_session._download_handshake_nonce(local_session.key_id,
                                                remote_hash)
        assert not local_session._handshake_error.called
        assert local_session.send.called

        remote_session._download_handshake_nonce(remote_session.key_id,
                                                 local_hash)
        assert not remote_session._handshake_error.called
        assert remote_session.send.called

        # Check self-issued nonce only. Asserts make sure that nonces
        # were verified successfully.

        msg_from_local = local_session.send.call_args[0][0]
        msg_from_remote = remote_session.send.call_args[0][0]

        local_nonce = msg_from_local.nonce
        remote_nonce = msg_from_remote.nonce

        local_session._react_to_resource_handshake_nonce(msg_from_remote)
        remote_session._react_to_resource_handshake_nonce(msg_from_local)

        local_session._react_to_resource_handshake_verdict(
            message.ResourceHandshakeVerdict(remote_nonce, accepted=True)
        )

        remote_session._react_to_resource_handshake_verdict(
            message.ResourceHandshakeVerdict(local_nonce, accepted=True)
        )

        assert local_session._finalize_handshake.called
        assert remote_session._finalize_handshake.called

        assert not local_session._handshake_error.called
        assert not remote_session._handshake_error.called

        assert not local_session.disconnect.called
        assert not remote_session.disconnect.called

    def test_share_handshake_nonce(self, *_):
        session = MockTaskSession(self.tempdir)
        self.__create_task_server(session)

        handshake = ResourceHandshake(self.key_id)
        handshake.start(self.tempdir)

        session._set_handshake(session.key_id, handshake)
        session._share_handshake_nonce(session.key_id)

        assert session.send.called
        msg = session.send.call_args[0][0]
        assert isinstance(msg, message.ResourceHandshakeStart)

    def __create_task_server(self, session):
        from golem.clientconfigdescriptor import ClientConfigDescriptor
        from golem.task.taskserver import TaskServer

        client = Mock(datadir=session.data_dir)

        resource_manager = HyperdriveResourceManager(
            dir_manager=DirManager(session.data_dir)
        )

        task_server = TaskServer(
            node=Mock(client=client, key=str(uuid.uuid4())),
            config_desc=ClientConfigDescriptor(),
            keys_auth=Mock(),
            client=client,
            use_docker_machine_manager=False
        )

        task_server.task_manager = Mock(
            task_result_manager=Mock(
                resource_manager=resource_manager
            )
        )

        peers = ['127.0.0.1:3282']
        client_options = HyperdriveClientOptions(
            HyperdriveClient.CLIENT_ID,
            HyperdriveClient.VERSION,
            options=dict(
                peers=peers,
                filtered=peers
            )
        )

        task_server.get_share_options = Mock(return_value=client_options)
        task_server.get_download_options = Mock(return_value=client_options)

        original_pull = resource_manager.pull_resource

        def pull_resource(*args, **kwargs):
            kwargs['async'] = False
            original_pull(*args[1:], **kwargs)

        resource_manager.pull_resource = types.MethodType(pull_resource,
                                                          resource_manager)
        session.task_server = task_server


class MockTaskSession(ResourceHandshakeSessionMixin):

    def __init__(self, data_dir,
                 successful_downloads=True, successful_uploads=True, **kwargs):

        ResourceHandshakeSessionMixin.__init__(self)

        dir_manager = DirManager(data_dir)
        get_dir = dir_manager.get_task_resource_dir

        self.send = Mock()
        self.disconnect = Mock()
        self.dropped = Mock()

        self.content_to_pull = str(uuid.uuid4())
        self.successful_downloads = successful_downloads
        self.successful_uploads = successful_uploads

        self.key_id = str(uuid.uuid4())
        self.data_dir = data_dir
        self.task_server = Mock(
            client=Mock(datadir=data_dir),
            node=Mock(key=str(uuid.uuid4())),
            deny_set=set(),
            resource_handshakes=dict(),
            task_manager=Mock(
                task_result_manager=Mock(
                    resource_manager=Mock(
                        storage=ResourceStorage(dir_manager, get_dir),
                        add_file=self.__add_file,
                        pull_resource=self.__pull_resource
                    )
                )
            )
        )

    def __add_file(self, path, task_id, absolute_path=False, client=None,
                   client_options=None):

        if not self.successful_uploads:
            raise RuntimeError('Test exception')
        return path, str(uuid.uuid4())

    def __pull_resource(self, entry, task_id, success, error, **kwargs):
        file_resource = entry[0]

        if not self.successful_downloads:
            return error(RuntimeError('Test exception'))

        directory = self.resource_manager.storage.get_dir(task_id)
        path = os.path.join(directory, file_resource.file_name)

        with open(path, 'w') as f:
            f.write(self.content_to_pull)

        return success((file_resource.file_name, file_resource.hash))


