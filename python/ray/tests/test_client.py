import os
import pytest
import time
import sys
import logging
import threading
import _thread

import ray.util.client.server.server as ray_client_server
from ray.tests.client_test_utils import create_remote_signal_actor
from ray.util.client.common import ClientObjectRef
from ray.util.client.ray_client_helpers import connect_to_client_or_not
from ray.util.client.ray_client_helpers import ray_start_client_server
from ray._private.client_mode_hook import client_mode_should_convert
from ray._private.client_mode_hook import enable_client_mode


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_client_thread_safe(call_ray_stop_only):
    import ray
    ray.init(num_cpus=2)

    with ray_start_client_server() as ray:

        @ray.remote
        def block():
            print("blocking run")
            time.sleep(99)

        @ray.remote
        def fast():
            print("fast run")
            return "ok"

        class Blocker(threading.Thread):
            def __init__(self):
                threading.Thread.__init__(self)
                self.daemon = True

            def run(self):
                ray.get(block.remote())

        b = Blocker()
        b.start()
        time.sleep(1)

        # Can concurrently execute the get.
        assert ray.get(fast.remote(), timeout=5) == "ok"


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_interrupt_ray_get(call_ray_stop_only):
    import ray
    ray.init(num_cpus=2)

    with ray_start_client_server() as ray:

        @ray.remote
        def block():
            print("blocking run")
            time.sleep(99)

        @ray.remote
        def fast():
            print("fast run")
            time.sleep(1)
            return "ok"

        class Interrupt(threading.Thread):
            def run(self):
                time.sleep(2)
                _thread.interrupt_main()

        it = Interrupt()
        it.start()
        with pytest.raises(KeyboardInterrupt):
            ray.get(block.remote())

        # Assert we can still get new items after the interrupt.
        assert ray.get(fast.remote()) == "ok"


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_real_ray_fallback(ray_start_regular_shared):
    with ray_start_client_server() as ray:

        @ray.remote
        def get_nodes_real():
            import ray as real_ray
            return real_ray.nodes()

        nodes = ray.get(get_nodes_real.remote())
        assert len(nodes) == 1, nodes

        @ray.remote
        def get_nodes():
            # Can access the full Ray API in remote methods.
            return ray.nodes()

        nodes = ray.get(get_nodes.remote())
        assert len(nodes) == 1, nodes


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_nested_function(ray_start_regular_shared):
    with ray_start_client_server() as ray:

        @ray.remote
        def g():
            @ray.remote
            def f():
                return "OK"

            return ray.get(f.remote())

        assert ray.get(g.remote()) == "OK"


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_put_get(ray_start_regular_shared):
    with ray_start_client_server() as ray:
        objectref = ray.put("hello world")
        print(objectref)

        retval = ray.get(objectref)
        assert retval == "hello world"
        # Make sure ray.put(1) == 1 is False and does not raise an exception.
        objectref = ray.put(1)
        assert not objectref == 1
        # Make sure it returns True when necessary as well.
        assert objectref == ClientObjectRef(objectref.id)


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_put_failure_get(ray_start_regular_shared):
    with ray_start_client_server() as ray:

        class DeSerializationFailure:
            def __getstate__(self):
                return ""

            def __setstate__(self, i):
                raise ZeroDivisionError

        dsf = DeSerializationFailure()
        with pytest.raises(ZeroDivisionError):
            ray.put(dsf)

        # Ensure Ray Client is still connected
        assert ray.get(ray.put(100)) == 100


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_wait(ray_start_regular_shared):
    with ray_start_client_server() as ray:
        objectref = ray.put("hello world")
        ready, remaining = ray.wait([objectref])
        assert remaining == []
        retval = ray.get(ready[0])
        assert retval == "hello world"

        objectref2 = ray.put(5)
        ready, remaining = ray.wait([objectref, objectref2])
        assert (ready, remaining) == ([objectref], [objectref2]) or \
            (ready, remaining) == ([objectref2], [objectref])
        ready_retval = ray.get(ready[0])
        remaining_retval = ray.get(remaining[0])
        assert (ready_retval, remaining_retval) == ("hello world", 5) \
            or (ready_retval, remaining_retval) == (5, "hello world")

        with pytest.raises(Exception):
            # Reference not in the object store.
            ray.wait([ClientObjectRef(b"blabla")])
        with pytest.raises(TypeError):
            ray.wait("blabla")
        with pytest.raises(TypeError):
            ray.wait(ClientObjectRef("blabla"))
        with pytest.raises(TypeError):
            ray.wait(["blabla"])


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_remote_functions(ray_start_regular_shared):
    with ray_start_client_server() as ray:
        SignalActor = create_remote_signal_actor(ray)
        signaler = SignalActor.remote()

        @ray.remote
        def plus2(x):
            return x + 2

        @ray.remote
        def fact(x):
            print(x, type(fact))
            if x <= 0:
                return 1
            # This hits the "nested tasks" issue
            # https://github.com/ray-project/ray/issues/3644
            # So we're on the right track!
            return ray.get(fact.remote(x - 1)) * x

        ref2 = plus2.remote(234)
        # `236`
        assert ray.get(ref2) == 236

        ref3 = fact.remote(20)
        # `2432902008176640000`
        assert ray.get(ref3) == 2_432_902_008_176_640_000

        # Reuse the cached ClientRemoteFunc object
        ref4 = fact.remote(5)
        assert ray.get(ref4) == 120

        # Test ray.wait()
        ref5 = fact.remote(10)
        # should return ref2, ref3, ref4
        res = ray.wait([ref5, ref2, ref3, ref4], num_returns=3)
        assert [ref2, ref3, ref4] == res[0]
        assert [ref5] == res[1]
        assert ray.get(res[0]) == [236, 2_432_902_008_176_640_000, 120]
        # should return ref2, ref3, ref4, ref5
        res = ray.wait([ref2, ref3, ref4, ref5], num_returns=4)
        assert [ref2, ref3, ref4, ref5] == res[0]
        assert [] == res[1]
        all_vals = ray.get(res[0])
        assert all_vals == [236, 2_432_902_008_176_640_000, 120, 3628800]

        # Timeout 0 on ray.wait leads to immediate return
        # (not indefinite wait for first return as with timeout None):
        unready_ref = signaler.wait.remote()
        res = ray.wait([unready_ref], timeout=0)
        # Not ready.
        assert res[0] == [] and len(res[1]) == 1
        ray.get(signaler.send.remote())
        ready_ref = signaler.wait.remote()
        # Ready.
        res = ray.wait([ready_ref], timeout=10)
        assert len(res[0]) == 1 and res[1] == []


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_function_calling_function(ray_start_regular_shared):
    with ray_start_client_server() as ray:

        @ray.remote
        def g():
            return "OK"

        @ray.remote
        def f():
            print(f, g)
            return ray.get(g.remote())

        print(f, type(f))
        assert ray.get(f.remote()) == "OK"


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_basic_actor(ray_start_regular_shared):
    with ray_start_client_server() as ray:

        @ray.remote
        class HelloActor:
            def __init__(self):
                self.count = 0

            def say_hello(self, whom):
                self.count += 1
                return ("Hello " + whom, self.count)

        actor = HelloActor.remote()
        s, count = ray.get(actor.say_hello.remote("you"))
        assert s == "Hello you"
        assert count == 1
        s, count = ray.get(actor.say_hello.remote("world"))
        assert s == "Hello world"
        assert count == 2


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_pass_handles(ray_start_regular_shared):
    """Test that passing client handles to actors and functions to remote actors
    in functions (on the server or raylet side) works transparently to the
    caller.
    """
    with ray_start_client_server() as ray:

        @ray.remote
        class ExecActor:
            def exec(self, f, x):
                return ray.get(f.remote(x))

            def exec_exec(self, actor, f, x):
                return ray.get(actor.exec.remote(f, x))

        @ray.remote
        def fact(x):
            out = 1
            while x > 0:
                out = out * x
                x -= 1
            return out

        @ray.remote
        def func_exec(f, x):
            return ray.get(f.remote(x))

        @ray.remote
        def func_actor_exec(actor, f, x):
            return ray.get(actor.exec.remote(f, x))

        @ray.remote
        def sneaky_func_exec(obj, x):
            return ray.get(obj["f"].remote(x))

        @ray.remote
        def sneaky_actor_exec(obj, x):
            return ray.get(obj["actor"].exec.remote(obj["f"], x))

        def local_fact(x):
            if x <= 0:
                return 1
            return x * local_fact(x - 1)

        assert ray.get(fact.remote(7)) == local_fact(7)
        assert ray.get(func_exec.remote(fact, 8)) == local_fact(8)
        test_obj = {}
        test_obj["f"] = fact
        assert ray.get(sneaky_func_exec.remote(test_obj, 5)) == local_fact(5)
        actor_handle = ExecActor.remote()
        assert ray.get(actor_handle.exec.remote(fact, 7)) == local_fact(7)
        assert ray.get(func_actor_exec.remote(actor_handle, fact,
                                              10)) == local_fact(10)
        second_actor = ExecActor.remote()
        assert ray.get(actor_handle.exec_exec.remote(second_actor, fact,
                                                     9)) == local_fact(9)
        test_actor_obj = {}
        test_actor_obj["actor"] = second_actor
        test_actor_obj["f"] = fact
        assert ray.get(sneaky_actor_exec.remote(test_actor_obj,
                                                4)) == local_fact(4)


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_basic_log_stream(ray_start_regular_shared):
    with ray_start_client_server() as ray:
        log_msgs = []

        def test_log(level, msg):
            log_msgs.append(msg)

        ray.worker.log_client.log = test_log
        ray.worker.log_client.set_logstream_level(logging.DEBUG)
        # Allow some time to propogate
        time.sleep(1)
        x = ray.put("Foo")
        assert ray.get(x) == "Foo"
        time.sleep(1)
        logs_with_id = [msg for msg in log_msgs if msg.find(x.id.hex()) >= 0]
        assert len(logs_with_id) >= 2
        assert any((msg.find("get") >= 0 for msg in logs_with_id))
        assert any((msg.find("put") >= 0 for msg in logs_with_id))


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_stdout_log_stream(ray_start_regular_shared):
    with ray_start_client_server() as ray:
        log_msgs = []

        def test_log(level, msg):
            log_msgs.append(msg)

        ray.worker.log_client.stdstream = test_log

        @ray.remote
        def print_on_stderr_and_stdout(s):
            print(s)
            print(s, file=sys.stderr)

        time.sleep(1)
        print_on_stderr_and_stdout.remote("Hello world")
        time.sleep(1)
        assert len(log_msgs) == 2
        assert all((msg.find("Hello world") for msg in log_msgs))


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_serializing_exceptions(ray_start_regular_shared):
    with ray_start_client_server() as ray:
        with pytest.raises(ValueError):
            ray.get_actor("abc")


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_create_remote_before_start(ray_start_regular_shared):
    """Creates remote objects (as though in a library) before
    starting the client.
    """
    from ray.util.client import ray

    @ray.remote
    class Returner:
        def doit(self):
            return "foo"

    @ray.remote
    def f(x):
        return x + 20

    # Prints in verbose tests
    print("Created remote functions")

    with ray_start_client_server() as ray:
        assert ray.get(f.remote(3)) == 23
        a = Returner.remote()
        assert ray.get(a.doit.remote()) == "foo"


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_basic_named_actor(ray_start_regular_shared):
    """Test that ray.get_actor() can create and return a detached actor.
    """
    with ray_start_client_server() as ray:

        @ray.remote
        class Accumulator:
            def __init__(self):
                self.x = 0

            def inc(self):
                self.x += 1

            def get(self):
                return self.x

        # Create the actor
        actor = Accumulator.options(name="test_acc").remote()

        actor.inc.remote()
        actor.inc.remote()

        # Make sure the get_actor call works
        new_actor = ray.get_actor("test_acc")
        new_actor.inc.remote()
        assert ray.get(new_actor.get.remote()) == 3

        del actor

        actor = Accumulator.options(
            name="test_acc2", lifetime="detached").remote()
        actor.inc.remote()
        del actor

        detatched_actor = ray.get_actor("test_acc2")
        for i in range(5):
            detatched_actor.inc.remote()

        assert ray.get(detatched_actor.get.remote()) == 6


def test_error_serialization(ray_start_regular_shared):
    """Test that errors will be serialized properly."""
    fake_path = os.path.join(os.path.dirname(__file__), "not_a_real_file")
    with pytest.raises(FileNotFoundError):
        with ray_start_client_server() as ray:

            @ray.remote
            def g():
                with open(fake_path, "r") as f:
                    f.read()

            # Raises a FileNotFoundError
            ray.get(g.remote())


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_internal_kv(ray_start_regular_shared):
    with ray_start_client_server() as ray:
        assert ray._internal_kv_initialized()
        assert not ray._internal_kv_put("apple", "b")
        assert ray._internal_kv_put("apple", "asdf")
        assert ray._internal_kv_put("apple", "b")
        assert ray._internal_kv_get("apple") == b"b"
        assert ray._internal_kv_put("apple", "asdf", overwrite=True)
        assert ray._internal_kv_get("apple") == b"asdf"
        assert ray._internal_kv_list("a") == [b"apple"]
        ray._internal_kv_del("apple")
        assert ray._internal_kv_get("apple") == b""


def test_startup_retry(ray_start_regular_shared):
    from ray.util.client import ray as ray_client
    ray_client._inside_client_test = True

    with pytest.raises(ConnectionError):
        ray_client.connect("localhost:50051", connection_retries=1)

    def run_client():
        ray_client.connect("localhost:50051")
        ray_client.disconnect()

    thread = threading.Thread(target=run_client, daemon=True)
    thread.start()
    time.sleep(3)
    server = ray_client_server.serve("localhost:50051")
    thread.join()
    server.stop(0)
    ray_client._inside_client_test = False


def test_dataclient_server_drop(ray_start_regular_shared):
    from ray.util.client import ray as ray_client
    ray_client._inside_client_test = True

    @ray_client.remote
    def f(x):
        time.sleep(4)
        return x

    def stop_server(server):
        time.sleep(2)
        server.stop(0)

    server = ray_client_server.serve("localhost:50051")
    ray_client.connect("localhost:50051")
    thread = threading.Thread(target=stop_server, args=(server, ))
    thread.start()
    x = f.remote(2)
    with pytest.raises(ConnectionError):
        _ = ray_client.get(x)
    thread.join()
    ray_client.disconnect()
    ray_client._inside_client_test = False
    # Wait for f(x) to finish before ray.shutdown() in the fixture
    time.sleep(3)


#@pytest.mark.skipif(sys.platform == "win32", reason="Failing on Windows.")
def test_client_gpu_ids(call_ray_stop_only):
    import ray
    ray.init(num_cpus=2)

    with enable_client_mode():
        # No client connection.
        with pytest.raises(Exception) as e:
            ray.get_gpu_ids()
        assert str(e.value) == "Ray Client is not connected."\
            " Please connect by calling `ray.connect`."

        with ray_start_client_server():
            # Now have a client connection.
            assert ray.get_gpu_ids() == []


def test_client_serialize_addon(call_ray_stop_only):
    import ray
    import pydantic

    ray.init(num_cpus=0)

    class User(pydantic.BaseModel):
        name: str

    with ray_start_client_server() as ray:
        assert ray.get(ray.put(User(name="ray"))).name == "ray"


@pytest.mark.parametrize("connect_to_client", [False, True])
def test_client_context_manager(ray_start_regular_shared, connect_to_client):
    import ray
    with connect_to_client_or_not(connect_to_client):
        if connect_to_client:
            # Client mode is on.
            assert client_mode_should_convert() is True
            # We're connected to Ray client.
            assert ray.util.client.ray.is_connected() is True
        else:
            assert client_mode_should_convert() is False
            assert ray.util.client.ray.is_connected() is False


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
