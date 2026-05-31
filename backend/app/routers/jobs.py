from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from ..db import get_db
from ..models import TTSJob
from ..routers.tts import serialize_job
from ..schemas import JobOut

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=list[JobOut])
def list_jobs(db: Session = Depends(get_db)):
    return [serialize_job(j) for j in db.query(TTSJob).options(joinedload(TTSJob.voice)).order_by(TTSJob.created_at.desc()).limit(200).all()]


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)):
    job = db.query(TTSJob).options(joinedload(TTSJob.voice)).filter(TTSJob.id == job_id).first()
    if not job: raise HTTPException(status_code=404, detail="Job not found")
    return serialize_job(job)


@router.delete("/{job_id}")
def delete_job(job_id: str, db: Session = Depends(get_db)):
    job = db.get(TTSJob, job_id)
    if not job: raise HTTPException(status_code=404, detail="Job not found")
    db.delete(job); db.commit()
    return {"ok": True}
