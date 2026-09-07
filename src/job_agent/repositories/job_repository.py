from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

from job_agent.domain.models import (
    ApplicationState,
    CandidateFact,
    JobInput,
    ScoreBreakdown,
    ScoreConfig,
)
from job_agent.domain.workflow import assert_transition


def _now() -> str: return datetime.now(UTC).isoformat()
def _url(url: str) -> str: return url.split("?")[0].rstrip("/")


class JobRepository:
    def __init__(self, connection: sqlite3.Connection): self.connection = connection
    def get_or_create_profile(self, display_name: str = "Candidate", email: str = "", phone: str = "", location: str = "") -> int:
        row = self.connection.execute("SELECT id FROM candidate_profiles ORDER BY id LIMIT 1").fetchone()
        if row:
            if any((email, phone, location)):
                self.connection.execute("UPDATE candidate_profiles SET display_name=?,email=?,phone=?,location=?,updated_at=? WHERE id=?", (display_name,email,phone,location,_now(),row["id"]))
                self.connection.commit()
            return row["id"]
        cur=self.connection.execute("INSERT INTO candidate_profiles(display_name,email,phone,location) VALUES (?,?,?,?)",(display_name,email,phone,location)); self.connection.commit(); return cur.lastrowid
    def add_fact(self, profile_id:int, fact:CandidateFact)->int:
        cur=self.connection.execute("INSERT INTO profile_facts(profile_id,fact_type,value,evidence,approved,active) VALUES (?,?,?,?,?,?)",(profile_id,fact.fact_type,fact.value,fact.evidence,int(fact.approved),int(fact.active))); self.connection.commit(); return cur.lastrowid
    def set_fact_active(self, fact_id:int, active:bool)->None:
        self.connection.execute("UPDATE profile_facts SET active=? WHERE id=?",(int(active),fact_id)); self._audit("profile_fact",fact_id,"FACT_REVOKED" if not active else "FACT_REACTIVATED","user",{}); self.connection.commit()
    def facts(self)->list[CandidateFact]:
        rows=self.connection.execute("SELECT id,fact_type,value,evidence,approved,active FROM profile_facts ORDER BY id").fetchall()
        return [CandidateFact(r["fact_type"],r["value"],r["evidence"],bool(r["approved"]),r["id"],bool(r["active"])) for r in rows]
    def _source(self, job:JobInput)->int|None:
        if not job.source_key:return None
        self.connection.execute("INSERT OR IGNORE INTO job_sources(name,kind,config_json) VALUES (?,?,?)",(job.source_key,job.source,json.dumps({"board_token":job.source_key})))
        return self.connection.execute("SELECT id FROM job_sources WHERE name=?",(job.source_key,)).fetchone()["id"]
    def add_job(self, job:JobInput)->tuple[int,bool]:
        source_id=self._source(job); normalized=_url(job.url)
        if source_id and job.external_id:
            row=self.connection.execute("SELECT id FROM jobs WHERE source_id=? AND external_id=?",(source_id,job.external_id)).fetchone()
        else: row=self.connection.execute("SELECT id FROM jobs WHERE url=?",(normalized,)).fetchone()
        if row:return row["id"],False
        fingerprint=hashlib.sha256(f"{job.source}|{job.source_key}|{job.external_id or normalized}".encode()).hexdigest()
        cur=self.connection.execute("INSERT INTO jobs(source_id,external_id,title,company,location,url,description,fingerprint) VALUES (?,?,?,?,?,?,?,?)",(source_id,job.external_id,job.title,job.company,job.location,normalized,job.description,fingerprint))
        jid=cur.lastrowid; self.connection.execute("INSERT INTO job_snapshots(job_id,raw_payload,normalized_json) VALUES (?,?,?)",(jid,job.raw_payload,json.dumps(job.__dict__))); self._audit("job",jid,"JOB_DISCOVERED","system",{"source":job.source}); self.connection.commit(); return jid,True
    def screen_and_assess(self, job_id:int, score:ScoreBreakdown, extraction:dict|None=None)->None:
        state=self.connection.execute("SELECT state FROM jobs WHERE id=?",(job_id,)).fetchone()["state"]
        if state not in {ApplicationState.DISCOVERED,ApplicationState.SCREENED}: return
        rec="DO NOT APPLY" if not score.eligibility_passed else "STRONG MATCH" if score.total>=85 else "POSSIBLE MATCH" if score.total>=70 else "WEAK MATCH"
        payload={"score":score.total,"components":score.__dict__}; self.connection.execute("INSERT INTO screenings(job_id,eligibility_passed,hard_rule_results_json) VALUES (?,?,?)",(job_id,int(score.eligibility_passed),json.dumps({"gates":score.hard_gate_reasons}))); self.connection.execute("INSERT INTO match_assessments(job_id,score,recommendation,score_json,extraction_json) VALUES (?,?,?,?,?)",(job_id,score.total,rec,json.dumps(payload,default=list),json.dumps(extraction or {}))); self.connection.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=?",(ApplicationState.SCREENED,_now(),job_id)); self.connection.commit()
    def create_application(self, job_id:int)->int|None:
        eligibility=self.connection.execute("SELECT eligibility_passed FROM screenings WHERE job_id=? ORDER BY id DESC LIMIT 1",(job_id,)).fetchone()
        if eligibility and not eligibility["eligibility_passed"]: return None
        row=self.connection.execute("SELECT id FROM applications WHERE job_id=?",(job_id,)).fetchone()
        if row:return row["id"]
        cur=self.connection.execute("INSERT INTO applications(job_id,state) VALUES (?,?)",(job_id,ApplicationState.REVIEW)); aid=cur.lastrowid; self.connection.execute("UPDATE jobs SET state=? WHERE id=?",(ApplicationState.REVIEW,job_id)); self._event(aid,None,ApplicationState.REVIEW,"system","Queued"); self.connection.commit(); return aid
    def transition(self, aid:int,target:ApplicationState,actor:str,reason:str="")->None:
        row=self.connection.execute("SELECT state,job_id FROM applications WHERE id=?",(aid,)).fetchone(); current=ApplicationState(row["state"]); assert_transition(current,target)
        artifact_ids = []
        if target == ApplicationState.APPROVED:
            artifact_ids = [r["id"] for r in self.connection.execute("SELECT a.id FROM application_artifacts a WHERE a.application_id=? AND a.version=(SELECT MAX(b.version) FROM application_artifacts b WHERE b.application_id=a.application_id AND b.kind=a.kind)", (aid,))]
        self.connection.execute("UPDATE applications SET state=?,updated_at=?,approved_at=CASE WHEN ?='APPROVED' THEN ? ELSE approved_at END,approved_artifact_ids_json=CASE WHEN ?='APPROVED' THEN ? ELSE approved_artifact_ids_json END WHERE id=?",(target,_now(),target,_now(),target,json.dumps(artifact_ids),aid)); self.connection.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=?",(target,_now(),row["job_id"])); self._event(aid,current,target,actor,reason); self._audit("application",aid,"STATE_CHANGED",actor,{"from":current,"to":target,"artifact_ids":artifact_ids}); self.connection.commit()
    def add_artifact(self,aid:int,kind:str,content:str,fact_ids:list[int])->int:
        version=self.connection.execute("SELECT COALESCE(MAX(version),0)+1 n FROM application_artifacts WHERE application_id=? AND kind=?",(aid,kind)).fetchone()["n"]; cur=self.connection.execute("INSERT INTO application_artifacts(application_id,kind,content,source_fact_ids_json,version) VALUES (?,?,?,?,?)",(aid,kind,content,json.dumps(fact_ids),version)); self._audit("artifact",cur.lastrowid,"ARTIFACT_CREATED","user",{"version":version,"facts":fact_ids}); self.connection.commit(); return cur.lastrowid
    def review_queue(self): return self.connection.execute("SELECT a.id application_id,a.state,j.title,j.company,j.location,j.url,COALESCE((SELECT score FROM match_assessments m WHERE m.job_id=j.id ORDER BY id DESC LIMIT 1),0) score,COALESCE((SELECT recommendation FROM match_assessments m WHERE m.job_id=j.id ORDER BY id DESC LIMIT 1),'PENDING') recommendation FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.state IN ('REVIEW','MATERIALS_READY','APPROVED','READY_TO_APPLY') ORDER BY a.updated_at DESC").fetchall()
    def blocked_jobs(self): return self.connection.execute("SELECT j.title,j.company,s.hard_rule_results_json FROM jobs j JOIN screenings s ON s.job_id=j.id WHERE s.id=(SELECT max(id) FROM screenings WHERE job_id=j.id) AND s.eligibility_passed=0").fetchall()
    def artifacts(self,aid:int): return self.connection.execute("SELECT id,kind,content,version,source_fact_ids_json FROM application_artifacts WHERE application_id=? ORDER BY kind,version DESC",(aid,)).fetchall()
    def events(self,aid:int): return self.connection.execute("SELECT * FROM application_events WHERE application_id=? ORDER BY id",(aid,)).fetchall()
    def dashboard_counts(self): return {r["state"]:r["count"] for r in self.connection.execute("SELECT state,count(*) count FROM jobs GROUP BY state")}
    def get_score_config(self) -> ScoreConfig:
        row=self.connection.execute("SELECT value_json FROM user_settings WHERE key='score_config'").fetchone()
        return ScoreConfig(**json.loads(row["value_json"])) if row else ScoreConfig()
    def save_score_config(self, config: ScoreConfig) -> None:
        self.connection.execute("INSERT INTO user_settings(key,value_json,updated_at) VALUES ('score_config',?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",(json.dumps(config.__dict__),_now())); self.connection.commit()
    def _event(self,aid,old,new,actor,reason): self.connection.execute("INSERT INTO application_events(application_id,from_state,to_state,actor,reason) VALUES (?,?,?,?,?)",(aid,old,new,actor,reason))
    def _audit(self,t,i,e,a,d): self.connection.execute("INSERT INTO audit_events(entity_type,entity_id,event_type,actor,details_json) VALUES (?,?,?,?,?)",(t,i,e,a,json.dumps(d,default=str)))
