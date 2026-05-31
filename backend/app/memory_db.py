import uuid
from datetime import datetime


class MemoryDB:
    """Vercel 无服务器环境用内存数据库"""

    def __init__(self):
        self.voices = {}
        self.jobs = {}
        self.users = {}

    def create_voice(self, **kwargs):
        vid = str(uuid.uuid4())
        kwargs["id"] = vid
        kwargs.setdefault("created_at", datetime.utcnow())
        kwargs.setdefault("updated_at", datetime.utcnow())
        kwargs.setdefault("status", "processing")
        self.voices[vid] = kwargs
        return kwargs

    def get_voice(self, vid):
        return self.voices.get(vid)

    def update_voice(self, vid, **kwargs):
        if vid in self.voices:
            kwargs["updated_at"] = datetime.utcnow()
            self.voices[vid].update(kwargs)
            return self.voices[vid]
        return None

    def delete_voice(self, vid):
        self.voices.pop(vid, None)

    def list_voices(self):
        return sorted(self.voices.values(), key=lambda v: v.get("created_at", ""), reverse=True)

    def create_job(self, **kwargs):
        jid = str(uuid.uuid4())
        kwargs["id"] = jid
        kwargs.setdefault("created_at", datetime.utcnow())
        kwargs.setdefault("status", "queued")
        self.jobs[jid] = kwargs
        return kwargs

    def get_job(self, jid):
        return self.jobs.get(jid)

    def update_job(self, jid, **kwargs):
        if jid in self.jobs:
            self.jobs[jid].update(kwargs)
            return self.jobs[jid]
        return None

    def delete_job(self, jid):
        self.jobs.pop(jid, None)

    def list_jobs(self):
        return sorted(self.jobs.values(), key=lambda j: j.get("created_at", ""), reverse=True)


db = MemoryDB()
