class Job:
    def __init__(self, job_id, priority, payload=None):
        if not job_id:
            raise ValueError('job_id must be non-empty')
        if priority < 0:
            raise ValueError('priority must be non-negative')
        self.job_id = job_id
        self.priority = priority
        self.payload = payload
