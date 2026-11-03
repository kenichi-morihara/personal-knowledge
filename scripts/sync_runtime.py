"""Independent, retryable Git and AI Tasks export workers."""
import threading


class SyncWorkers:
    def __init__(self, store, tasks, interval=30):
        self.stop = threading.Event()
        self.events = [threading.Event(), threading.Event()]
        self.threads = []
        for operation, wake in zip((store.sync, tasks.sync), self.events):
            def work(operation=operation, wake=wake):
                while not self.stop.is_set():
                    try:
                        operation()
                    except Exception:
                        # State is already durable. Never log data/credentials; retry later.
                        pass
                    wake.wait(interval)
                    wake.clear()
            thread = threading.Thread(target=work, daemon=True)
            thread.start()
            self.threads.append(thread)

    def request(self):
        for event in self.events:
            event.set()

    def close(self):
        self.stop.set()
        self.request()
