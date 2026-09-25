class Job:
    def __init__(self, job_id, priority, payload=None):
        # BUG: no validation
        self.job_id = job_id
        self.priority = priority
        self.payload = payload
