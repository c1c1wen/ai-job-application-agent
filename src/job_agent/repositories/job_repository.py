from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime

from job_agent.domain.models import (
    ApplicationState,
    CandidateFact,
    ExecutionState,
    JobInput,
    ScoreBreakdown,
    ScoreConfig,
)
from job_agent.domain.workflow import assert_execution_transition, assert_transition
from job_agent.services.profile_reconciliation import (
    fact_core_identity,
    fact_identity,
    fact_supported_by_text,
)


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
    def profile(self): return self.connection.execute("SELECT * FROM candidate_profiles ORDER BY id LIMIT 1").fetchone()
    def add_fact(self, profile_id:int, fact:CandidateFact)->int:
        cur=self.connection.execute("INSERT INTO profile_facts(profile_id,fact_type,value,evidence,approved,active) VALUES (?,?,?,?,?,?)",(profile_id,fact.fact_type,fact.value,fact.evidence,int(fact.approved),int(fact.active))); self.connection.commit(); return cur.lastrowid
    def add_pending_fact(self, profile_id:int, fact:CandidateFact, document_id:int, snippet:str, confidence:str)->int:
        existing=self.connection.execute("SELECT id FROM profile_facts WHERE profile_id=? AND fact_type=? AND value=? AND active=1",(profile_id,fact.fact_type,fact.value)).fetchone()
        if existing:return existing["id"]
        cur=self.connection.execute("INSERT INTO profile_facts(profile_id,fact_type,value,evidence,approved,active,status,source_document_id,source_snippet,confidence) VALUES (?,?,?,?,0,1,'PENDING',?,?,?)",(profile_id,fact.fact_type,fact.value,fact.evidence,document_id,snippet,confidence)); self.connection.execute("INSERT INTO profile_fact_versions(fact_id,value,evidence,status,actor) VALUES (?,?,?,'PENDING','ai_extraction')",(cur.lastrowid,fact.value,fact.evidence)); self.connection.commit(); return cur.lastrowid
    def _record_cv_fact_source(self, fact_id: int, document_id: int, identity_key: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO profile_fact_sources(fact_id,source_document_id,identity_key,relation) VALUES (?,?,?,'CV')",
            (fact_id, document_id, identity_key),
        )
    def add_cv_fact(self, profile_id:int, fact:CandidateFact, document_id:int, snippet:str, confidence:str)->int:
        """Explicit CV facts are trusted immediately; provenance remains attached for audit."""
        existing=self.connection.execute("SELECT id,status FROM profile_facts WHERE profile_id=? AND fact_type=? AND value=? AND active=1",(profile_id,fact.fact_type,fact.value)).fetchone()
        if existing:
            if existing["status"] == "PENDING": self.review_fact(existing["id"],"APPROVED")
            self._record_cv_fact_source(existing["id"], document_id, fact_identity(fact.fact_type, fact.value))
            self.connection.commit()
            return existing["id"]
        cur=self.connection.execute("INSERT INTO profile_facts(profile_id,fact_type,value,evidence,approved,active,status,source_document_id,source_snippet,confidence) VALUES (?,?,?,?,1,1,'APPROVED',?,?,?)",(profile_id,fact.fact_type,fact.value,fact.evidence,document_id,snippet,confidence)); self.connection.execute("INSERT INTO profile_fact_versions(fact_id,value,evidence,status,actor) VALUES (?,?,?,'APPROVED','cv_extraction')",(cur.lastrowid,fact.value,fact.evidence)); self._record_cv_fact_source(cur.lastrowid, document_id, fact_identity(fact.fact_type, fact.value)); self.connection.commit(); return cur.lastrowid
    def reconcile_cv_facts(self, profile_id: int, document_id: int, extracted: list[dict]) -> dict[str, int]:
        """Reconcile one complete canonical CV extraction without deleting history.

        Exact semantic identities are retained; core identity matches (same
        degree/employer and role) are versioned when dates or wording change.
        Only facts previously marked as CV-derived participate in retirement.
        """
        if not extracted:
            raise ValueError("A CV reconciliation requires extracted facts; the existing profile was unchanged.")
        source_document = self.connection.execute(
            "SELECT text_content FROM source_documents WHERE id=?", (document_id,)
        ).fetchone()
        if not source_document:
            raise ValueError("The CV source document was not found.")
        rows = self.connection.execute(
            "SELECT f.* FROM profile_facts f JOIN profile_fact_sources p ON p.fact_id=f.id "
            "WHERE f.profile_id=? AND f.active=1 AND p.relation='CV' GROUP BY f.id ORDER BY f.id",
            (profile_id,),
        ).fetchall()
        by_core: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_core.setdefault(fact_core_identity(row["fact_type"], row["value"]), []).append(row)
        seen: set[int] = set()
        result = {"added": 0, "updated": 0, "unchanged": 0, "deactivated": 0, "duplicates": 0}
        for item in extracted:
            fact = item["fact"]
            core = fact_core_identity(fact.fact_type, fact.value)
            identity = fact_identity(fact.fact_type, fact.value)
            matches = by_core.get(core, [])
            if matches:
                primary, *duplicates = matches
                seen.add(primary["id"])
                changed = (
                    primary["fact_type"] != fact.fact_type
                    or primary["value"] != fact.value
                    or primary["evidence"] != fact.evidence
                    or primary["source_document_id"] != document_id
                )
                if changed:
                    self.connection.execute(
                        "UPDATE profile_facts SET fact_type=?,value=?,evidence=?,approved=1,active=1,status='APPROVED',"
                        "source_document_id=?,source_snippet=?,confidence=? WHERE id=?",
                        (fact.fact_type, fact.value, fact.evidence, document_id, item["snippet"], item["confidence"], primary["id"]),
                    )
                    self.connection.execute(
                        "INSERT INTO profile_fact_versions(fact_id,value,evidence,status,actor) VALUES (?,?,?,'APPROVED','cv_reconciliation')",
                        (primary["id"], fact.value, fact.evidence),
                    )
                    self._audit(
                        "profile_fact", primary["id"], "CV_FACT_RECONCILED", "system",
                        {"identity": identity, "source_document_id": document_id},
                    )
                    result["updated"] += 1
                else:
                    result["unchanged"] += 1
                self._record_cv_fact_source(primary["id"], document_id, identity)
                for duplicate in duplicates:
                    if duplicate["id"] in seen:
                        continue
                    seen.add(duplicate["id"])
                    self._deactivate_cv_fact(duplicate["id"], "DUPLICATE_CV_FACT_SUPPRESSED", identity)
                    result["duplicates"] += 1
                continue
            cur = self.connection.execute(
                "INSERT INTO profile_facts(profile_id,fact_type,value,evidence,approved,active,status,source_document_id,source_snippet,confidence) "
                "VALUES (?,?,?,?,1,1,'APPROVED',?,?,?)",
                (profile_id, fact.fact_type, fact.value, fact.evidence, document_id, item["snippet"], item["confidence"]),
            )
            fact_id = cur.lastrowid
            self.connection.execute(
                "INSERT INTO profile_fact_versions(fact_id,value,evidence,status,actor) VALUES (?,?,?,'APPROVED','cv_reconciliation')",
                (fact_id, fact.value, fact.evidence),
            )
            self._record_cv_fact_source(fact_id, document_id, identity)
            self._audit("profile_fact", fact_id, "CV_FACT_ADDED", "system", {"identity": identity, "source_document_id": document_id})
            seen.add(fact_id)
            result["added"] += 1
        for row in rows:
            if row["id"] not in seen:
                if fact_supported_by_text(
                    row["fact_type"], row["value"], source_document["text_content"]
                ):
                    self.connection.execute(
                        "UPDATE profile_facts SET source_document_id=? WHERE id=?",
                        (document_id, row["id"]),
                    )
                    self._record_cv_fact_source(
                        row["id"], document_id, fact_identity(row["fact_type"], row["value"])
                    )
                    self._audit(
                        "profile_fact", row["id"], "CV_FACT_CURRENT_SOURCE_CONFIRMED", "system",
                        {"source_document_id": document_id},
                    )
                    continue
                self._deactivate_cv_fact(row["id"], "CV_FACT_NO_LONGER_SUPPORTED", fact_identity(row["fact_type"], row["value"]))
                result["deactivated"] += 1
        self.connection.commit()
        return result
    def _deactivate_cv_fact(self, fact_id: int, event_type: str, identity: str) -> None:
        row = self.connection.execute("SELECT value,evidence FROM profile_facts WHERE id=?", (fact_id,)).fetchone()
        self.connection.execute("UPDATE profile_facts SET active=0,status='REVOKED' WHERE id=?", (fact_id,))
        self.connection.execute(
            "INSERT INTO profile_fact_versions(fact_id,value,evidence,status,actor) VALUES (?,?,?,'REVOKED','cv_reconciliation')",
            (fact_id, row["value"], row["evidence"]),
        )
        self._audit("profile_fact", fact_id, event_type, "system", {"identity": identity})
    def reconcile_stored_cv_document(
        self, profile_id: int, document_id: int, extracted_from_document_id: int | None = None
    ) -> dict[str, int]:
        """Reconcile an already extracted CV document during a safe one-time cleanup."""
        extracted_from_document_id = extracted_from_document_id or document_id
        rows = self.connection.execute(
            "SELECT fact_type,value,evidence,source_snippet,confidence FROM profile_facts "
            "WHERE profile_id=? AND source_document_id=? AND active=1 ORDER BY id",
            (profile_id, extracted_from_document_id),
        ).fetchall()
        extracted = [
            {
                "fact": CandidateFact(row["fact_type"], row["value"], row["evidence"]),
                "snippet": row["source_snippet"] or row["evidence"],
                "confidence": row["confidence"] or "high",
            }
            for row in rows
        ]
        return self.reconcile_cv_facts(profile_id, document_id, extracted)
    def promote_stale_cv_facts(self) -> int:
        rows=self.connection.execute("SELECT id FROM profile_facts WHERE status='PENDING' AND source_document_id IS NOT NULL AND source_snippet IS NOT NULL AND length(trim(source_snippet))>0").fetchall()
        for row in rows: self.review_fact(row["id"],"APPROVED")
        return len(rows)
    def add_document(self, filename:str, path:str, mime_type:str, text:str)->int:
        cur=self.connection.execute("INSERT INTO source_documents(filename,stored_path,mime_type,text_content) VALUES (?,?,?,?)",(filename,path,mime_type,text)); self.connection.commit(); return cur.lastrowid
    def review_fact(self, fact_id:int, status:str, value:str|None=None, evidence:str|None=None)->None:
        row=self.connection.execute("SELECT value,evidence FROM profile_facts WHERE id=?",(fact_id,)).fetchone(); value=value or row["value"]; evidence=evidence or row["evidence"]; active=status=="APPROVED"
        self.connection.execute("UPDATE profile_facts SET value=?,evidence=?,approved=?,active=?,status=? WHERE id=?",(value,evidence,int(status=="APPROVED"),int(active),status,fact_id)); self.connection.execute("INSERT INTO profile_fact_versions(fact_id,value,evidence,status,actor) VALUES (?,?,?,?,?)",(fact_id,value,evidence,status,"user")); self._audit("profile_fact",fact_id,"FACT_"+status,"user",{}); self.connection.commit()
    def review_facts(self, fact_ids:list[int], status:str)->None:
        for fact_id in fact_ids: self.review_fact(fact_id,status)
    def set_fact_active(self, fact_id:int, active:bool)->None:
        self.connection.execute("UPDATE profile_facts SET active=?,status=? WHERE id=?",(int(active),"APPROVED" if active else "REVOKED",fact_id)); self._audit("profile_fact",fact_id,"FACT_REVOKED" if not active else "FACT_REACTIVATED","user",{}); self.connection.commit()
    def facts(self)->list[CandidateFact]:
        rows=self.connection.execute("SELECT id,fact_type,value,evidence,approved,active FROM profile_facts ORDER BY id").fetchall()
        return [CandidateFact(r["fact_type"],r["value"],r["evidence"],bool(r["approved"]),r["id"],bool(r["active"])) for r in rows]
    def fact_review_rows(self):
        return self.connection.execute("SELECT f.id,f.fact_type,f.value,f.status,f.evidence,f.source_snippet,f.confidence,f.source_document_id,d.filename FROM profile_facts f LEFT JOIN source_documents d ON d.id=f.source_document_id ORDER BY f.id DESC").fetchall()
    def _source(self, job:JobInput)->int|None:
        if not job.source_key:return None
        self.connection.execute("INSERT OR IGNORE INTO job_sources(name,kind,config_json) VALUES (?,?,?)",(job.source_key,job.source,json.dumps({"board_token":job.source_key})))
        return self.connection.execute("SELECT id FROM job_sources WHERE name=?",(job.source_key,)).fetchone()["id"]
    def add_job(self, job:JobInput)->tuple[int,bool]:
        source_id=self._source(job); normalized=job.url.rstrip("/") if job.source == "ADZUNA" else _url(job.url)
        if source_id and job.external_id:
            row=self.connection.execute("SELECT id FROM jobs WHERE source_id=? AND external_id=?",(source_id,job.external_id)).fetchone()
        else: row=self.connection.execute("SELECT id FROM jobs WHERE url=?",(normalized,)).fetchone()
        if row:
            self.connection.execute("UPDATE jobs SET title=?,company=?,location=?,url=?,description=?,updated_at=? WHERE id=?",(job.title,job.company,job.location,normalized,job.description,_now(),row["id"]))
            self.connection.commit()
            return row["id"],False
        fingerprint=hashlib.sha256(f"{job.source}|{job.source_key}|{job.external_id or normalized}".encode()).hexdigest()
        cur=self.connection.execute("INSERT INTO jobs(source_id,external_id,title,company,location,url,description,fingerprint) VALUES (?,?,?,?,?,?,?,?)",(source_id,job.external_id,job.title,job.company,job.location,normalized,job.description,fingerprint))
        jid=cur.lastrowid; self.connection.execute("INSERT INTO job_snapshots(job_id,raw_payload,normalized_json) VALUES (?,?,?)",(jid,job.raw_payload,json.dumps(job.__dict__))); self._audit("job",jid,"JOB_DISCOVERED","system",{"source":job.source}); self.connection.commit(); return jid,True
    def screen_and_assess(self, job_id:int, score:ScoreBreakdown, extraction:dict|None=None)->None:
        state=self.connection.execute("SELECT state FROM jobs WHERE id=?",(job_id,)).fetchone()["state"]
        config = self.get_score_config()
        rec="DO NOT APPLY" if not score.eligibility_passed else "STRONG MATCH" if score.total>=config.strong_match_threshold else "GOOD MATCH" if score.total>=config.good_match_threshold else "POSSIBLE MATCH" if score.total>=config.possible_match_threshold else "WEAK MATCH"
        payload={"score":score.total,"components":score.__dict__}; self.connection.execute("INSERT INTO screenings(job_id,eligibility_passed,hard_rule_results_json) VALUES (?,?,?)",(job_id,int(score.eligibility_passed),json.dumps({"gates":score.hard_gate_reasons}))); self.connection.execute("INSERT INTO match_assessments(job_id,score,recommendation,score_json,extraction_json) VALUES (?,?,?,?,?)",(job_id,score.total,rec,json.dumps(payload,default=list),json.dumps(extraction or {})))
        if state in {ApplicationState.DISCOVERED, ApplicationState.SCREENED}:
            self.connection.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=?",(ApplicationState.SCREENED,_now(),job_id))
        self.connection.commit()
    def record_search_eligibility(self, job_id:int, eligible:bool, reasons:tuple[str,...], config_hash:str)->None:
        self.connection.execute("INSERT INTO job_search_eligibility(job_id,eligible,reasons_json,config_hash,evaluated_at) VALUES (?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET eligible=excluded.eligible,reasons_json=excluded.reasons_json,config_hash=excluded.config_hash,evaluated_at=excluded.evaluated_at",(job_id,int(eligible),json.dumps(reasons),config_hash,_now()))
        self._audit("job",job_id,"SEARCH_ELIGIBILITY_EVALUATED","system",{"eligible":eligible,"reasons":reasons}); self.connection.commit()
    def opportunity_evaluation_provenance(self) -> dict[str, int | str | None]:
        """Return a stable fingerprint of evidence allowed in an opportunity brief.

        This deliberately contains only approved, active profile facts plus the
        canonical-CV version.  It is not an application-document fingerprint:
        application-specific CV refresh remains its own explicit workflow.
        """
        profile = self.profile()
        facts = [
            {
                "id": fact.id,
                "fact_type": fact.fact_type,
                "value": fact.value,
                "evidence": fact.evidence,
            }
            for fact in self.facts()
            if fact.approved and fact.active
        ]
        payload = {
            "profile": {
                "display_name": profile["display_name"] if profile else "",
                "email": profile["email"] if profile else "",
                "phone": profile["phone"] if profile else "",
                "location": profile["location"] if profile else "",
            },
            "facts": sorted(facts, key=lambda item: item["id"]),
        }
        canonical = self.current_canonical_cv()
        return {
            "profile_snapshot_hash": hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "canonical_cv_id": canonical["id"] if canonical else None,
        }

    def opportunity_evaluation_status(self, job_id: int) -> dict[str, object]:
        """Return the stored brief and whether its candidate evidence is current."""
        stored = self.opportunity_evaluation(job_id)
        current = self.opportunity_evaluation_provenance()
        stale = bool(
            stored
            and (
                stored["profile_snapshot_hash"] != current["profile_snapshot_hash"]
                or stored["canonical_cv_id"] != current["canonical_cv_id"]
            )
        )
        return {"stored": stored, "current": current, "stale": stale}

    def save_opportunity_evaluation(
        self,
        job_id: int,
        evaluation: dict,
        tailoring: dict,
        narrative: dict | None = None,
        *,
        refresh_stale: bool = False,
    ) -> bool:
        """Persist a decision brief without modifying score or workflow state.

        A brief generated from superseded candidate evidence is never silently
        replaced.  An explicit re-evaluation archives it, clears its dependent
        narrative, and records a new current revision.
        """
        evaluation_json = json.dumps(evaluation, sort_keys=True)
        tailoring_json = json.dumps(tailoring, sort_keys=True)
        narrative_json = json.dumps(narrative, sort_keys=True) if narrative else None
        existing = self.opportunity_evaluation(job_id)
        provenance = self.opportunity_evaluation_provenance()
        stale = bool(
            existing
            and (
                existing["profile_snapshot_hash"] != provenance["profile_snapshot_hash"]
                or existing["canonical_cv_id"] != provenance["canonical_cv_id"]
            )
        )
        if stale and not refresh_stale:
            return False
        now = _now()
        if stale:
            self.connection.execute(
                "INSERT INTO opportunity_evaluation_history("
                "job_id,revision,evaluation_json,narrative_json,tailoring_json,profile_snapshot_hash,"
                "canonical_cv_id,evaluated_at,narrative_generated_at,archive_reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, existing["revision"], existing["evaluation_json"],
                    existing["narrative_json"], existing["tailoring_json"],
                    existing["profile_snapshot_hash"], existing["canonical_cv_id"],
                    existing["evaluated_at"], existing["narrative_generated_at"],
                    "PROFILE_OR_CANONICAL_CV_CHANGED",
                ),
            )
            self.connection.execute(
                "UPDATE opportunity_evaluations SET evaluation_json=?,narrative_json=NULL,tailoring_json=?,"
                "evaluated_at=?,narrative_generated_at=NULL,profile_snapshot_hash=?,canonical_cv_id=?,revision=? "
                "WHERE job_id=?",
                (
                    evaluation_json, tailoring_json, now, provenance["profile_snapshot_hash"],
                    provenance["canonical_cv_id"], existing["revision"] + 1, job_id,
                ),
            )
            self._audit(
                "job", job_id, "OPPORTUNITY_REEVALUATED", "user",
                {"revision": existing["revision"] + 1, "narrative": False},
            )
            self.connection.commit()
            return True
        if (
            existing
            and existing["evaluation_json"] == evaluation_json
            and existing["tailoring_json"] == tailoring_json
            and (narrative_json is None or existing["narrative_json"] == narrative_json)
        ):
            return False
        self.connection.execute(
            "INSERT INTO opportunity_evaluations("
            "job_id,evaluation_json,narrative_json,tailoring_json,evaluated_at,narrative_generated_at,"
            "profile_snapshot_hash,canonical_cv_id,revision) "
            "VALUES (?,?,?,?,?,CASE WHEN ? IS NULL THEN NULL ELSE ? END,?,?,1) "
            "ON CONFLICT(job_id) DO UPDATE SET evaluation_json=excluded.evaluation_json, "
            "tailoring_json=excluded.tailoring_json, "
            "narrative_json=CASE WHEN opportunity_evaluations.evaluation_json=excluded.evaluation_json "
            "AND opportunity_evaluations.tailoring_json=excluded.tailoring_json "
            "THEN COALESCE(excluded.narrative_json,opportunity_evaluations.narrative_json) ELSE excluded.narrative_json END, "
            "evaluated_at=excluded.evaluated_at, "
            "narrative_generated_at=CASE WHEN excluded.narrative_json IS NULL "
            "THEN CASE WHEN opportunity_evaluations.evaluation_json=excluded.evaluation_json "
            "AND opportunity_evaluations.tailoring_json=excluded.tailoring_json "
            "THEN opportunity_evaluations.narrative_generated_at ELSE NULL END "
            "ELSE excluded.narrative_generated_at END, "
            "profile_snapshot_hash=excluded.profile_snapshot_hash,canonical_cv_id=excluded.canonical_cv_id",
            (
                job_id, evaluation_json, narrative_json, tailoring_json, now, narrative_json, now,
                provenance["profile_snapshot_hash"], provenance["canonical_cv_id"],
            ),
        )
        self._audit("job", job_id, "OPPORTUNITY_EVALUATED", "user", {"narrative": bool(narrative)})
        self.connection.commit()
        return True
    def opportunity_evaluation(self, job_id: int):
        return self.connection.execute(
            "SELECT * FROM opportunity_evaluations WHERE job_id=?", (job_id,)
        ).fetchone()
    def opportunity_evaluation_history(self, job_id: int):
        return self.connection.execute(
            "SELECT * FROM opportunity_evaluation_history WHERE job_id=? ORDER BY revision DESC,id DESC",
            (job_id,),
        ).fetchall()
    def jobs_for_search_evaluation(self):
        return self.connection.execute("SELECT j.id,j.title,j.company,j.location,j.url,j.description,COALESCE(s.kind,'MANUAL') source,s.name source_key,j.external_id FROM jobs j LEFT JOIN job_sources s ON s.id=j.source_id").fetchall()
    def job_for_search_evaluation(self, job_id: int):
        return self.connection.execute("SELECT j.id,j.title,j.company,j.location,j.url,j.description,COALESCE(s.kind,'MANUAL') source,s.name source_key,j.external_id FROM jobs j LEFT JOIN job_sources s ON s.id=j.source_id WHERE j.id=?", (job_id,)).fetchone()
    def enrichment(self, job_id: int):
        return self.connection.execute("SELECT * FROM job_enrichments WHERE job_id=?", (job_id,)).fetchone()
    def record_enrichment(self, job_id: int, enrichment) -> None:
        self.connection.execute("INSERT INTO job_enrichments(job_id,discovery_source,original_job_url,enrichment_url,enrichment_source,description,jd_status,evidence_quality,retrieval_status,retrieved_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET enrichment_url=excluded.enrichment_url,enrichment_source=excluded.enrichment_source,description=excluded.description,jd_status=excluded.jd_status,evidence_quality=excluded.evidence_quality,retrieval_status=excluded.retrieval_status,retrieved_at=excluded.retrieved_at", (job_id, "ADZUNA", enrichment.original_job_url, enrichment.enrichment_url, enrichment.enrichment_source, enrichment.description, enrichment.jd_status, enrichment.evidence_quality, enrichment.retrieval_status, _now()))
        if enrichment.jd_status == "FULL" and enrichment.description:
            self.connection.execute("UPDATE jobs SET description=?,updated_at=? WHERE id=?", (enrichment.description, _now(), job_id))
        self._audit("job", job_id, "JOB_JD_ENRICHED", "system", {"status": enrichment.jd_status, "source": enrichment.enrichment_source, "retrieval": enrichment.retrieval_status})
        self.connection.commit()
    def upsert_employer_ats(self, canonical_employer: str, aliases: list[str], careers_domain: str, ats_provider: str | None, ats_identifier: str | None, evidence: dict) -> int:
        self.connection.execute(
            "INSERT INTO employer_ats_registry(canonical_employer,aliases_json,careers_domain,ats_provider,ats_identifier,verified_at,verification_evidence_json) VALUES (?,?,?,?,?,?,?) ON CONFLICT(canonical_employer) DO UPDATE SET aliases_json=excluded.aliases_json,careers_domain=excluded.careers_domain,ats_provider=excluded.ats_provider,ats_identifier=excluded.ats_identifier,verified_at=excluded.verified_at,verification_evidence_json=excluded.verification_evidence_json",
            (canonical_employer, json.dumps(aliases), careers_domain, ats_provider, ats_identifier, _now(), json.dumps(evidence)),
        )
        self.connection.commit()
        return self.connection.execute("SELECT id FROM employer_ats_registry WHERE canonical_employer=?", (canonical_employer,)).fetchone()["id"]
    def employer_ats_registry(self):
        return self.connection.execute("SELECT * FROM employer_ats_registry ORDER BY canonical_employer").fetchall()
    def record_discovery_run(self, query: dict, funnel: dict[str, int], sources: dict[str, dict]) -> int:
        cur = self.connection.execute(
            "INSERT INTO discovery_runs(query_json,funnel_json) VALUES (?,?)",
            (json.dumps(query), json.dumps(funnel)),
        )
        run_id = cur.lastrowid
        for source, values in sources.items():
            self.connection.execute(
                "INSERT INTO discovery_run_sources(run_id,source,raw_count,new_count,previously_seen_count,likely_eligible_count,possible_count,eligible_count,good_possible_count,direct_verified_count,live_count,error_message) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, source, values.get("raw", 0), values.get("new", 0), values.get("previously_seen", 0), values.get("likely_eligible", 0), values.get("possible", 0), values.get("eligible", 0), values.get("good_or_possible", 0), values.get("direct_verified", 0), values.get("live", 0), values.get("error")),
            )
        self.connection.commit()
        return run_id
    def latest_discovery_run(self):
        return self.connection.execute("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT 1").fetchone()
    def discovery_run_sources(self, run_id: int):
        return self.connection.execute("SELECT * FROM discovery_run_sources WHERE run_id=? ORDER BY source", (run_id,)).fetchall()
    def record_discovery_observations(self, run_id: int, observations: list[dict]) -> None:
        self.connection.executemany(
            "INSERT OR REPLACE INTO discovery_job_observations(run_id,job_id,freshness,source_kind,preliminary_status,resolution_status) VALUES (?,?,?,?,?,?)",
            [
                (
                    run_id,
                    item["job_id"],
                    item["freshness"],
                    item["source_kind"],
                    item.get("preliminary_status"),
                    item.get("resolution_status"),
                )
                for item in observations
            ],
        )
        self.connection.commit()

    def discovery_inbox(self, run_id: int):
        """Fresh and previously-seen records from one run, without debug payloads."""
        return self.connection.execute(
            "SELECT o.freshness,o.source_kind,o.preliminary_status,o.resolution_status,j.id job_id,j.title,j.company,j.location,j.url,j.discovered_at,COALESCE(s.kind,'MANUAL') source,COALESCE(m.score,0) score,COALESCE(m.recommendation,'NOT SCORED') recommendation,COALESCE(m.score_json,'{}') score_json,a.id application_id,a.state application_state,ar.application_source,ar.canonical_application_url FROM discovery_job_observations o JOIN jobs j ON j.id=o.job_id LEFT JOIN job_sources s ON s.id=j.source_id LEFT JOIN match_assessments m ON m.id=(SELECT id FROM match_assessments WHERE job_id=j.id ORDER BY id DESC LIMIT 1) LEFT JOIN applications a ON a.job_id=j.id LEFT JOIN job_application_resolutions ar ON ar.job_id=j.id WHERE o.run_id=? ORDER BY o.freshness='NEW' DESC,m.score DESC,j.title",
            (run_id,),
        ).fetchall()
    def record_application_resolution(self, job_id: int, resolution) -> None:
        job = self.connection.execute("SELECT j.url,COALESCE(s.kind,'MANUAL') source FROM jobs j LEFT JOIN job_sources s ON s.id=j.source_id WHERE j.id=?", (job_id,)).fetchone()
        if not job:
            raise ValueError("Job was not found.")
        self.connection.execute(
            "INSERT INTO job_application_resolutions(job_id,discovery_source,discovery_url,application_source,canonical_application_url,resolution_status,resolution_confidence,resolution_evidence_json,resolved_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET application_source=excluded.application_source,canonical_application_url=excluded.canonical_application_url,resolution_status=excluded.resolution_status,resolution_confidence=excluded.resolution_confidence,resolution_evidence_json=excluded.resolution_evidence_json,resolved_at=excluded.resolved_at",
            (job_id, job["source"], job["url"], resolution.application_source, resolution.canonical_application_url, resolution.status, resolution.confidence, json.dumps(resolution.evidence), _now()),
        )
        self._audit("job", job_id, "APPLICATION_SOURCE_RESOLVED", "system", {"status": resolution.status, "confidence": resolution.confidence, "application_source": resolution.application_source})
        self.connection.commit()
    def application_resolution(self, job_id: int):
        return self.connection.execute("SELECT * FROM job_application_resolutions WHERE job_id=?", (job_id,)).fetchone()
    def has_application(self, job_id: int) -> bool:
        return bool(self.connection.execute("SELECT 1 FROM applications WHERE job_id=?", (job_id,)).fetchone())
    def canonical_application_url(self, job_id: int) -> str | None:
        row = self.application_resolution(job_id)
        return row["canonical_application_url"] if row and row["resolution_status"] == "DIRECT_VERIFIED" else None
    def create_application(self, job_id:int)->int|None:
        eligibility=self.connection.execute("SELECT eligibility_passed FROM screenings WHERE job_id=? ORDER BY id DESC LIMIT 1",(job_id,)).fetchone()
        if eligibility and not eligibility["eligibility_passed"]: return None
        row=self.connection.execute("SELECT id,state FROM applications WHERE job_id=?",(job_id,)).fetchone()
        if row:
            self.connection.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=?",(row["state"],_now(),job_id)); self.connection.commit()
            return row["id"]
        cur=self.connection.execute("INSERT INTO applications(job_id,state) VALUES (?,?)",(job_id,ApplicationState.REVIEW)); aid=cur.lastrowid; self.connection.execute("UPDATE jobs SET state=? WHERE id=?",(ApplicationState.REVIEW,job_id)); self._event(aid,None,ApplicationState.REVIEW,"system","Queued"); self.connection.commit(); return aid
    def add_application_history(self, company: str, role_title: str, *, application_url: str | None = None, job_external_id: str | None = None, location: str | None = None, source: str | None = None, date_applied: str | None = None, status: str = "ALREADY_APPLIED", method: str = "MANUAL", notes: str | None = None) -> int:
        cur = self.connection.execute("INSERT INTO application_history(company,role_title,application_url,job_external_id,location,source,date_applied,status,method,notes) VALUES (?,?,?,?,?,?,?,?,?,?)", (company, role_title, application_url, job_external_id, location, source, date_applied, status, method, notes))
        self.connection.commit()
        return cur.lastrowid
    def application_history(self):
        return self.connection.execute("SELECT * FROM application_history ORDER BY COALESCE(date_applied,created_at) DESC,id DESC").fetchall()
    def refresh_duplicate_check(self, job_id: int):
        from job_agent.services.duplicate_detection import detect_duplicate
        candidate = self.connection.execute("SELECT id,title,company,location,url,external_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not candidate:
            raise ValueError("Job was not found.")
        history = [dict(row) for row in self.application_history()]
        applied = self.connection.execute("SELECT a.id,j.title,j.company,j.location,j.url,j.external_id FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.state='APPLIED' AND j.id!=?", (job_id,)).fetchall()
        history.extend({"id": row["id"], "title": row["title"], "company": row["company"], "location": row["location"], "url": row["url"], "job_external_id": row["external_id"], "method": "AGENT"} for row in applied)
        decision = detect_duplicate(dict(candidate), history)
        self.connection.execute("INSERT INTO job_duplicate_checks(job_id,status,reason,matched_history_id,checked_at) VALUES (?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,matched_history_id=excluded.matched_history_id,checked_at=excluded.checked_at", (job_id, decision.status, decision.reason, decision.history_id, _now()))
        self.connection.commit()
        return decision
    def duplicate_check(self, job_id: int):
        return self.connection.execute("SELECT * FROM job_duplicate_checks WHERE job_id=?", (job_id,)).fetchone()
    def transition(self, aid:int,target:ApplicationState,actor:str,reason:str="")->None:
        row=self.connection.execute("SELECT state,job_id FROM applications WHERE id=?",(aid,)).fetchone(); current=ApplicationState(row["state"]); assert_transition(current,target)
        artifact_ids = []
        if target == ApplicationState.APPROVED:
            artifact_ids = [r["id"] for r in self.connection.execute("SELECT a.id FROM application_artifacts a WHERE a.application_id=? AND a.version=(SELECT MAX(b.version) FROM application_artifacts b WHERE b.application_id=a.application_id AND b.kind=a.kind)", (aid,))]
        self.connection.execute("UPDATE applications SET state=?,updated_at=?,approved_at=CASE WHEN ?='APPROVED' THEN ? ELSE approved_at END,applied_at=CASE WHEN ?='APPLIED' THEN ? ELSE applied_at END,approved_artifact_ids_json=CASE WHEN ?='APPROVED' THEN ? ELSE approved_artifact_ids_json END WHERE id=?",(target,_now(),target,_now(),target,_now(),target,json.dumps(artifact_ids),aid)); self.connection.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=?",(target,_now(),row["job_id"])); self._event(aid,current,target,actor,reason); self._audit("application",aid,"STATE_CHANGED",actor,{"from":current,"to":target,"artifact_ids":artifact_ids}); self.connection.commit()
    def add_artifact(self,aid:int,kind:str,content:str,fact_ids:list[int],model_used:str="deterministic")->int:
        version=self.connection.execute("SELECT COALESCE(MAX(version),0)+1 n FROM application_artifacts WHERE application_id=? AND kind=?",(aid,kind)).fetchone()["n"]; cur=self.connection.execute("INSERT INTO application_artifacts(application_id,kind,content,source_fact_ids_json,version,model_used) VALUES (?,?,?,?,?,?)",(aid,kind,content,json.dumps(fact_ids),version,model_used)); self._audit("artifact",cur.lastrowid,"ARTIFACT_CREATED","user",{"version":version,"facts":fact_ids,"model_used":model_used}); self.connection.commit(); return cur.lastrowid
    def replace_approved_cover_letter(self, aid: int, content: str, fact_ids: list[int], model_used: str = "user_grounded_revision") -> int:
        """Version a user-confirmed cover-letter correction without resetting execution.

        The existing approval snapshot stays authoritative for all other
        materials. Only its cover-letter reference is moved to the newly
        created version, so an already-ready execution session can safely use
        the correction on its next document preparation pass.
        """
        application = self.connection.execute(
            "SELECT state,approved_artifact_ids_json FROM applications WHERE id=?", (aid,)
        ).fetchone()
        if not application or application["state"] not in {ApplicationState.APPROVED, ApplicationState.READY_TO_APPLY, ApplicationState.APPLYING, ApplicationState.HUMAN_VERIFICATION_REQUIRED}:
            raise ValueError("Only an approved application package can replace its active cover letter.")
        artifact_id = self.add_artifact(aid, "cover_letter", content, fact_ids, model_used)
        approved_ids = json.loads(application["approved_artifact_ids_json"])
        existing = self.connection.execute(
            f"SELECT id,kind FROM application_artifacts WHERE id IN ({','.join('?' for _ in approved_ids)})",
            approved_ids,
        ).fetchall() if approved_ids else []
        non_cover_ids = [row["id"] for row in existing if row["kind"] != "cover_letter"]
        new_snapshot = [artifact_id, *non_cover_ids]
        self.connection.execute(
            "UPDATE applications SET approved_artifact_ids_json=?,updated_at=? WHERE id=?",
            (json.dumps(new_snapshot), _now(), aid),
        )
        self._audit(
            "application",
            aid,
            "APPROVED_COVER_LETTER_REPLACED",
            "user",
            {"artifact_id": artifact_id, "replaced_artifact_ids": [row["id"] for row in existing if row["kind"] == "cover_letter"]},
        )
        self.connection.commit()
        return artifact_id
    def review_queue(self):
        """Return one best active representation of each cross-source opportunity.

        Source records and application history remain intact.  This only avoids
        making the candidate review the same employer/title repeatedly when a
        public ATS feed and an index both publish it.
        """
        rows = self.connection.execute("SELECT a.id application_id,a.state,j.id job_id,j.title,j.company,j.location,j.url,j.description,COALESCE(s.kind,'MANUAL') source,COALESCE((SELECT score FROM match_assessments m WHERE m.job_id=j.id ORDER BY id DESC LIMIT 1),0) score,COALESCE((SELECT recommendation FROM match_assessments m WHERE m.job_id=j.id ORDER BY id DESC LIMIT 1),'PENDING') recommendation,COALESCE((SELECT score_json FROM match_assessments m WHERE m.job_id=j.id ORDER BY id DESC LIMIT 1),'{}') score_json,COALESCE((SELECT extraction_json FROM match_assessments m WHERE m.job_id=j.id ORDER BY id DESC LIMIT 1),'{}') extraction_json,COALESCE(je.jd_status,CASE WHEN s.kind IN ('LEVER','GREENHOUSE') THEN 'FULL' ELSE 'PARTIAL' END) jd_status,COALESCE(je.evidence_quality,CASE WHEN s.kind IN ('LEVER','GREENHOUSE') THEN 'HIGH' ELSE 'PARTIAL' END) evidence_quality,COALESCE(je.enrichment_source,s.kind,'MANUAL') enrichment_source,je.original_job_url,je.enrichment_url,ar.application_source,ar.canonical_application_url,ar.resolution_status,ar.resolution_confidence,ar.resolution_evidence_json,(SELECT e.id FROM application_execution_sessions e WHERE e.application_id=a.id ORDER BY e.id DESC LIMIT 1) execution_session_id FROM applications a JOIN jobs j ON j.id=a.job_id LEFT JOIN job_sources s ON s.id=j.source_id LEFT JOIN job_search_eligibility e ON e.job_id=j.id LEFT JOIN job_enrichments je ON je.job_id=j.id LEFT JOIN job_application_resolutions ar ON ar.job_id=j.id WHERE a.state IN ('REVIEW','MATERIALS_READY','APPROVED','READY_TO_APPLY','APPLYING','HUMAN_VERIFICATION_REQUIRED') AND COALESCE(e.eligible,1)=1 ORDER BY score DESC,a.updated_at DESC").fetchall()
        selected = []
        for row in rows:
            duplicate = next((current for current in selected if self._same_active_opportunity(row, current)), None)
            if duplicate is None:
                selected.append(row)
            elif self._queue_priority(row) > self._queue_priority(duplicate):
                selected[selected.index(duplicate)] = row
        return sorted(selected, key=lambda row: row["score"], reverse=True)

    @staticmethod
    def _queue_priority(row) -> tuple[int, int, int, int]:
        """Keep a live application package attached when duplicate source records merge."""
        state_priority = {
            ApplicationState.READY_TO_APPLY: 4,
            ApplicationState.APPLYING: 4,
            ApplicationState.HUMAN_VERIFICATION_REQUIRED: 4,
            ApplicationState.APPROVED: 3,
            ApplicationState.MATERIALS_READY: 2,
            ApplicationState.REVIEW: 1,
        }
        return (
            int(row["execution_session_id"] is not None),
            state_priority.get(row["state"], 0),
            int(row["source"] in {"LEVER", "GREENHOUSE"}),
            row["score"],
        )
    @staticmethod
    def _same_active_opportunity(left, right) -> bool:
        clean = lambda value: re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()
        title_match = clean(left["title"]) == clean(right["title"])
        if not title_match:
            return False
        urls = {value for value in (left["url"], left["original_job_url"], left["enrichment_url"]) if value}
        if urls.intersection(value for value in (right["url"], right["original_job_url"], right["enrichment_url"]) if value):
            return True
        groups = {"dreamworld": "ardent leisure group", "ardent leisure group": "ardent leisure group"}
        company_match = groups.get(clean(left["company"]), clean(left["company"])) == groups.get(clean(right["company"]), clean(right["company"]))
        location_overlap = bool(set(clean(left["location"]).split()).intersection(clean(right["location"]).split()))
        words = lambda value: set(clean(value).split())
        first, second = words(left["description"]), words(right["description"])
        # A full ATS description can have many additional words compared with an
        # index summary.  Containment catches that relationship without treating
        # two merely keyword-overlapping descriptions as the same opportunity.
        similarity = len(first.intersection(second)) / max(1, min(len(first), len(second)))
        return company_match and location_overlap and similarity >= 0.80
    def blocked_jobs(self): return self.connection.execute("SELECT j.title,j.company,s.hard_rule_results_json FROM jobs j JOIN screenings s ON s.job_id=j.id WHERE s.id=(SELECT max(id) FROM screenings WHERE job_id=j.id) AND s.eligibility_passed=0").fetchall()
    def artifacts(self,aid:int): return self.connection.execute("SELECT id,kind,content,version,source_fact_ids_json,model_used,created_at FROM application_artifacts WHERE application_id=? ORDER BY kind,version DESC",(aid,)).fetchall()
    def approved_artifacts(self, aid: int):
        row = self.connection.execute("SELECT approved_artifact_ids_json FROM applications WHERE id=?", (aid,)).fetchone()
        if not row:
            return []
        ids = json.loads(row["approved_artifact_ids_json"])
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return self.connection.execute(
            f"SELECT id,kind,content,version,source_fact_ids_json,created_at FROM application_artifacts WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    def latest_candidate_cv(self):
        return self.connection.execute("SELECT d.id,d.filename,d.stored_path,d.mime_type FROM canonical_cv_versions c JOIN source_documents d ON d.id=c.source_document_id ORDER BY c.id DESC LIMIT 1").fetchone()
    def register_canonical_cv(self, source_path: str, filename: str, sha256: str, modified_ns: int, size_bytes: int, text: str):
        row = self.connection.execute("SELECT * FROM canonical_cv_versions WHERE sha256=?", (sha256,)).fetchone()
        if row:
            self.connection.execute("UPDATE canonical_cv_versions SET last_seen_at=? WHERE id=?", (_now(), row["id"]))
            self.connection.commit()
            return self.connection.execute("SELECT * FROM canonical_cv_versions WHERE id=?", (row["id"],)).fetchone()
        document_id = self.add_document(filename, source_path, "application/pdf", text)
        cur = self.connection.execute("INSERT INTO canonical_cv_versions(source_path,filename,sha256,modified_ns,size_bytes,source_document_id) VALUES (?,?,?,?,?,?)", (source_path, filename, sha256, modified_ns, size_bytes, document_id))
        self._audit("canonical_cv", cur.lastrowid, "CANONICAL_CV_REGISTERED", "user", {"filename": filename, "sha256": sha256, "source_document_id": document_id})
        self.connection.commit()
        return self.connection.execute("SELECT * FROM canonical_cv_versions WHERE id=?", (cur.lastrowid,)).fetchone()
    def current_canonical_cv(self):
        return self.connection.execute("SELECT c.*,d.mime_type,d.text_content FROM canonical_cv_versions c JOIN source_documents d ON d.id=c.source_document_id ORDER BY c.id DESC LIMIT 1").fetchone()
    def application_cv(self, aid: int):
        return self.connection.execute("SELECT a.*,c.source_path,c.modified_ns,c.size_bytes FROM application_cv_versions a JOIN canonical_cv_versions c ON c.id=a.canonical_cv_id WHERE a.application_id=? AND a.active=1 ORDER BY a.id DESC LIMIT 1", (aid,)).fetchone()
    def application_cv_document(self, aid: int):
        return self.connection.execute("SELECT a.source_document_id id,a.canonical_filename filename,a.stored_path,'application/pdf' mime_type FROM application_cv_versions a WHERE a.application_id=? AND a.active=1 ORDER BY a.id DESC LIMIT 1", (aid,)).fetchone()
    def application_cv_is_outdated(self, aid: int, canonical=None) -> bool:
        canonical = canonical or self.current_canonical_cv()
        attachment = self.application_cv(aid)
        return bool(canonical and (not attachment or attachment["sha256"] != canonical["sha256"]))
    def attach_application_cv(self, aid: int, canonical_cv_id: int, source_document_id: int, filename: str, sha256: str, stored_path: str) -> int:
        app = self.connection.execute("SELECT state FROM applications WHERE id=?", (aid,)).fetchone()
        if not app:
            raise ValueError("Application was not found.")
        if app["state"] == ApplicationState.APPLIED:
            raise ValueError("Submitted applications retain their historical CV version.")
        self.connection.execute("UPDATE application_cv_versions SET active=0 WHERE application_id=? AND active=1", (aid,))
        cur = self.connection.execute("INSERT INTO application_cv_versions(application_id,canonical_cv_id,source_document_id,canonical_filename,sha256,stored_path,active) VALUES (?,?,?,?,?,?,1)", (aid, canonical_cv_id, source_document_id, filename, sha256, stored_path))
        self._audit("application", aid, "APPLICATION_CV_UPDATED", "user", {"application_cv_id": cur.lastrowid, "canonical_cv_id": canonical_cv_id, "source_document_id": source_document_id, "sha256": sha256})
        self.connection.commit()
        return cur.lastrowid
    def start_execution(self, aid: int, platform: str, application_url: str) -> int:
        application = self.connection.execute("SELECT a.state,a.job_id,COALESCE(s.kind,'MANUAL') source FROM applications a JOIN jobs j ON j.id=a.job_id LEFT JOIN job_sources s ON s.id=j.source_id WHERE a.id=?", (aid,)).fetchone()
        if not application or application["state"] != ApplicationState.READY_TO_APPLY:
            raise ValueError("Only approved applications that are ready to apply can start execution.")
        if application["source"] == "ADZUNA":
            resolution = self.application_resolution(application["job_id"])
            if not resolution or resolution["resolution_status"] != "DIRECT_VERIFIED" or resolution["canonical_application_url"] != application_url:
                raise ValueError("Aggregator-discovered jobs require a verified direct employer application source before execution.")
        cur = self.connection.execute(
            "INSERT INTO application_execution_sessions(application_id,state,platform,application_url) VALUES (?,?,?,?)",
            (aid, "OPENING_APPLICATION", platform, application_url),
        )
        session_id = cur.lastrowid
        self._execution_event(session_id, "OPENING_APPLICATION", "Execution session started", {"platform": platform})
        self._audit("application_execution", session_id, "EXECUTION_STARTED", "user", {"application_id": aid, "platform": platform})
        self.connection.commit()
        return session_id
    def latest_execution(self, aid: int):
        return self.connection.execute("SELECT * FROM application_execution_sessions WHERE application_id=? ORDER BY id DESC LIMIT 1", (aid,)).fetchone()
    def execution_job(self, session_id: int):
        return self.connection.execute("SELECT j.id,j.title,j.company,j.location,j.url FROM application_execution_sessions e JOIN applications a ON a.id=e.application_id JOIN jobs j ON j.id=a.job_id WHERE e.id=?", (session_id,)).fetchone()
    def execution_uploads(self, session_id: int):
        return self.connection.execute("SELECT * FROM application_execution_uploads WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    def execution_events(self, session_id: int):
        return self.connection.execute("SELECT * FROM application_execution_events WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    def execution_diagnostics(self, session_id: int):
        return self.connection.execute("SELECT * FROM application_execution_diagnostics WHERE session_id=? ORDER BY id DESC", (session_id,)).fetchall()
    def browser_context(self, session_id: int):
        return self.connection.execute("SELECT * FROM execution_browser_contexts WHERE session_id=?", (session_id,)).fetchone()
    def record_browser_context(self, session_id: int, inspection) -> None:
        if not inspection.browser_worker_id:
            return
        self.connection.execute(
            "INSERT INTO execution_browser_contexts(session_id,worker_instance_id,context_id,page_id,current_url,platform,pageup_stage,cv_confirmed,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET worker_instance_id=excluded.worker_instance_id,context_id=excluded.context_id,page_id=excluded.page_id,current_url=excluded.current_url,platform=excluded.platform,pageup_stage=excluded.pageup_stage,cv_confirmed=excluded.cv_confirmed,last_seen_at=excluded.last_seen_at",
            (session_id, inspection.browser_worker_id, inspection.browser_context_id, inspection.browser_page_id, inspection.page_url, inspection.platform, inspection.pageup_stage or None, int(inspection.cv_confirmed), _now()),
        )
        self._execution_event(session_id, "BROWSER_CONTEXT", "Worker-owned browser context observed", {"worker_instance_id": inspection.browser_worker_id, "context_id": inspection.browser_context_id, "page_id": inspection.browser_page_id, "pageup_stage": inspection.pageup_stage, "cv_confirmed": inspection.cv_confirmed})
        self.connection.commit()
    def record_execution_diagnostic(self, session_id: int, phase: str, diagnostic, replayed_answer_count: int = 0) -> None:
        session = self.connection.execute("SELECT state FROM application_execution_sessions WHERE id=?", (session_id,)).fetchone()
        self.connection.execute(
            "INSERT INTO application_execution_diagnostics(session_id,phase,page_url,cv_attached,required_complete,missing_fields_json,visible_errors_json,human_verification,page_reloaded,replayed_answer_count,execution_state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, phase, diagnostic.page_url, int(diagnostic.cv_attached), int(diagnostic.required_complete), json.dumps(diagnostic.missing_fields), json.dumps(diagnostic.visible_errors), int(diagnostic.human_verification), diagnostic.page_reloaded, replayed_answer_count, session["state"] if session else None),
        )
        self._execution_event(session_id, "LIVE_VALIDATION", f"Live form validation: {phase}", {"cv_attached": diagnostic.cv_attached, "required_complete": diagnostic.required_complete, "missing_count": len(diagnostic.missing_fields), "error_count": len(diagnostic.visible_errors), "human_verification": diagnostic.human_verification})
        self.connection.commit()
    def execution_answers(self, session_id: int):
        return self.connection.execute("SELECT * FROM application_execution_answers WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    def answer_map(self, session_id: int) -> dict[str, object]:
        return {row["field_key"]: json.loads(row["value_json"]) for row in self.execution_answers(session_id)}
    def save_execution_answer(self, session_id: int, field: dict, value, category: str, reusable: bool) -> None:
        if category in {"sensitive", "legal"}:
            reusable = False
        self.connection.execute(
            "INSERT INTO application_execution_answers(session_id,field_key,field_name,label,field_type,value_json,category,reusable,updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id,field_key) DO UPDATE SET value_json=excluded.value_json,category=excluded.category,reusable=excluded.reusable,updated_at=excluded.updated_at",
            (session_id, field["key"], field["name"], field["label"], field["field_type"], json.dumps(value), category, int(reusable), _now()),
        )
        session = self.connection.execute("SELECT platform FROM application_execution_sessions WHERE id=?", (session_id,)).fetchone()
        if reusable:
            self.connection.execute(
                "INSERT INTO execution_reusable_answers(platform,field_key,label,field_type,value_json,category,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(platform,field_key) DO UPDATE SET value_json=excluded.value_json,label=excluded.label,field_type=excluded.field_type,category=excluded.category,updated_at=excluded.updated_at",
                (session["platform"], field["key"], field["label"], field["field_type"], json.dumps(value), category, _now()),
            )
        self._execution_event(session_id, "WAITING_FOR_USER", "User answer saved", {"field_key": field["key"], "category": category, "reusable": reusable})
        self._audit("application_execution", session_id, "USER_ANSWER_SAVED", "user", {"field_key": field["key"], "category": category, "reusable": reusable})
        self.connection.commit()
    def reusable_answer(self, platform: str, field_key: str):
        row = self.connection.execute("SELECT * FROM execution_reusable_answers WHERE platform=? AND field_key=?", (platform, field_key)).fetchone()
        return json.loads(row["value_json"]) if row else None
    def application_profile(self) -> dict[str, dict]:
        rows = self.connection.execute("SELECT field_key,value_json,category,reusable FROM application_profile_values ORDER BY field_key").fetchall()
        return {row["field_key"]: {"value": json.loads(row["value_json"]), "category": row["category"], "reusable": bool(row["reusable"])} for row in rows}
    def set_ats_authentication(self, ats: str, status: str, *, verified: bool = False) -> None:
        allowed = {"AUTHENTICATED", "LOGIN_REQUIRED", "SESSION_EXPIRED", "UNKNOWN"}
        if status not in allowed:
            raise ValueError("Unknown ATS authentication status.")
        verified_at = _now() if verified else None
        self.connection.execute(
            "INSERT INTO ats_authentication_status(ats,authentication_status,last_verified_at,last_checked_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(ats) DO UPDATE SET authentication_status=excluded.authentication_status,last_verified_at=CASE WHEN excluded.last_verified_at IS NOT NULL THEN excluded.last_verified_at ELSE ats_authentication_status.last_verified_at END,last_checked_at=excluded.last_checked_at,updated_at=excluded.updated_at",
            (ats, status, verified_at, _now(), _now()),
        )
        self._audit("ats_authentication", None, "ATS_AUTH_STATUS_UPDATED", "system", {"ats": ats, "status": status})
        self.connection.commit()
    def ats_authentication(self, ats: str):
        return self.connection.execute("SELECT * FROM ats_authentication_status WHERE ats=?", (ats,)).fetchone()
    def ats_authentication_states(self):
        return self.connection.execute("SELECT * FROM ats_authentication_status ORDER BY ats").fetchall()
    def needs_attention(self):
        """Return non-secret, resumable human checkpoints grouped by ATS in the UI."""
        return self.connection.execute(
            "SELECT e.id session_id,e.application_id,e.platform,e.state,e.warnings_json,e.updated_at,j.company,j.title FROM application_execution_sessions e JOIN applications a ON a.id=e.application_id JOIN jobs j ON j.id=a.job_id WHERE e.state IN ('WAITING_FOR_USER','HUMAN_VERIFICATION_REQUIRED') ORDER BY e.updated_at DESC"
        ).fetchall()
    def authentication_ready_sessions(self):
        """Only return sessions explicitly paused for login whose ATS was reverified."""
        return self.connection.execute(
            "SELECT e.* FROM application_execution_sessions e JOIN ats_authentication_status auth ON auth.ats=e.platform WHERE e.state='WAITING_FOR_USER' AND auth.authentication_status='AUTHENTICATED' AND e.warnings_json LIKE '%LOGIN_REQUIRED%' ORDER BY e.id"
        ).fetchall()
    def save_application_profile_value(self, field_key: str, value, category: str, reusable: bool) -> None:
        if field_key in {"privacy_consent", "terms_acceptance", "legal_declaration"}:
            raise ValueError("Privacy consents and legal declarations must remain per-application decisions.")
        self.connection.execute(
            "INSERT INTO application_profile_values(field_key,value_json,category,reusable,updated_at) VALUES (?,?,?,?,?) ON CONFLICT(field_key) DO UPDATE SET value_json=excluded.value_json,category=excluded.category,reusable=excluded.reusable,updated_at=excluded.updated_at",
            (field_key, json.dumps(value), category, int(reusable), _now()),
        )
        self._audit("application_profile", None, "APPLICATION_PROFILE_VALUE_SAVED", "user", {"field_key": field_key, "category": category, "reusable": reusable})
        self.connection.commit()
    def update_execution(
        self,
        session_id: int,
        state: str,
        step: str,
        detected_fields: list[dict] | None = None,
        autofilled_fields: list[dict] | None = None,
        unresolved_fields: list[dict] | None = None,
        warnings: list[str] | None = None,
        error_message: str | None = None,
    ) -> None:
        row = self.connection.execute("SELECT state FROM application_execution_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise ValueError("Application execution session was not found.")
        assert_execution_transition(ExecutionState(row["state"]), ExecutionState(state))
        self.connection.execute(
            "UPDATE application_execution_sessions SET state=?,detected_fields_json=?,autofilled_fields_json=?,unresolved_fields_json=?,warnings_json=?,error_message=?,updated_at=? WHERE id=?",
            (state, json.dumps(detected_fields or []), json.dumps(autofilled_fields or []), json.dumps(unresolved_fields or []), json.dumps(warnings or []), error_message, _now(), session_id),
        )
        self._execution_event(session_id, state, step, {"error": error_message} if error_message else {})
        self._audit("application_execution", session_id, "EXECUTION_UPDATED", "system", {"state": state, "step": step})
        self.connection.commit()
    def record_execution_upload(self, session_id: int, artifact_id: int | None, artifact_version: int | None, source_document_id: int | None, filename: str, upload_field: str | None) -> None:
        self.connection.execute("INSERT INTO application_execution_uploads(session_id,artifact_id,artifact_version,source_document_id,filename,upload_field) VALUES (?,?,?,?,?,?)", (session_id, artifact_id, artifact_version, source_document_id, filename, upload_field))
        self._execution_event(session_id, "FILLING_FORM", "Document upload/replay recorded", {"artifact_id": artifact_id, "source_document_id": source_document_id, "field": upload_field})
        self.connection.commit()
    def record_execution_answer_replay(self, session_id: int, answer_count: int) -> None:
        """Audit answer application without persisting answer values in event data."""
        self._execution_event(session_id, "RESUMING", "Saved answers replayed", {"answer_count": answer_count})
        self._audit("application_execution", session_id, "EXECUTION_ANSWERS_REPLAYED", "system", {"answer_count": answer_count})
        self.connection.commit()
    def record_execution_cv_refresh(self, session_id: int) -> None:
        session = self.connection.execute("SELECT application_id,state FROM application_execution_sessions WHERE id=?", (session_id,)).fetchone()
        if not session:
            raise ValueError("Application execution session was not found.")
        attachment = self.application_cv(session["application_id"])
        if not attachment:
            raise ValueError("An application CV must be selected before it can be recorded.")
        if session["state"] == ExecutionState.FAILED:
            stored = self.connection.execute("SELECT detected_fields_json,autofilled_fields_json,unresolved_fields_json,warnings_json FROM application_execution_sessions WHERE id=?", (session_id,)).fetchone()
            self.update_execution(
                session_id,
                ExecutionState.READY_FOR_FINAL_REVIEW,
                "Application CV updated; returned to final review without submission",
                json.loads(stored["detected_fields_json"]),
                json.loads(stored["autofilled_fields_json"]),
                json.loads(stored["unresolved_fields_json"]),
                json.loads(stored["warnings_json"]),
            )
        else:
            self._execution_event(session_id, session["state"], "Application CV updated to current canonical version", {"source_document_id": attachment["source_document_id"], "canonical_cv_id": attachment["canonical_cv_id"]})
        self._audit("application_execution", session_id, "APPLICATION_CV_REFRESHED", "user", {"source_document_id": attachment["source_document_id"], "canonical_cv_id": attachment["canonical_cv_id"]})
        self.connection.commit()
    def begin_final_submission(self, session_id: int) -> None:
        session = self.connection.execute("SELECT e.application_id,e.state,e.detected_fields_json,e.autofilled_fields_json,e.unresolved_fields_json,e.warnings_json,a.job_id FROM application_execution_sessions e JOIN applications a ON a.id=e.application_id WHERE e.id=?", (session_id,)).fetchone()
        if not session or session["state"] != ExecutionState.READY_FOR_FINAL_REVIEW:
            raise ValueError("Final submission is available only after final review is complete.")
        duplicate = self.refresh_duplicate_check(session["job_id"])
        if duplicate.status in {"DUPLICATE_CONFIRMED", "ALREADY_APPLIED", "DUPLICATE_POSSIBLE"}:
            raise ValueError(f"Submission stopped: {duplicate.status}. {duplicate.reason}")
        application = self.connection.execute("SELECT state FROM applications WHERE id=?", (session["application_id"],)).fetchone()
        if not application or application["state"] not in {ApplicationState.READY_TO_APPLY, ApplicationState.APPLYING}:
            raise ValueError("This application is not available for final submission.")
        if application["state"] == ApplicationState.READY_TO_APPLY:
            self.transition(session["application_id"], ApplicationState.APPLYING, "user", "User explicitly confirmed final submission")
        self.update_execution(session_id, ExecutionState.SUBMITTING, "Final submission explicitly confirmed by user", json.loads(session["detected_fields_json"]), json.loads(session["autofilled_fields_json"]), json.loads(session["unresolved_fields_json"]), json.loads(session["warnings_json"]))
    def mark_execution_submitted(self, session_id: int, message: str) -> None:
        session = self.connection.execute("SELECT application_id,state,detected_fields_json,autofilled_fields_json,unresolved_fields_json FROM application_execution_sessions WHERE id=?", (session_id,)).fetchone()
        if not session or session["state"] != ExecutionState.SUBMITTING:
            raise ValueError("A verified submission must follow the submitting state.")
        self.transition(session["application_id"], ApplicationState.APPLIED, "system", "Platform submission verified")
        self.update_execution(session_id, ExecutionState.APPLIED, message, json.loads(session["detected_fields_json"]), json.loads(session["autofilled_fields_json"]), json.loads(session["unresolved_fields_json"]))
    def save_for_later(self, aid: int) -> None:
        application = self.connection.execute(
            "SELECT state FROM applications WHERE id=?", (aid,)
        ).fetchone()
        if not application:
            raise ValueError("Application was not found.")
        # Saving is an explicit user action, not a state transition. Retain the
        # current state so the auditable event satisfies the non-null workflow
        # contract without changing approval or execution gates.
        self._event(aid, application["state"], application["state"], "user", "Saved for later")
        self._audit("application", aid, "SAVED_FOR_LATER", "user", {})
        self.connection.commit()
    def events(self,aid:int): return self.connection.execute("SELECT * FROM application_events WHERE application_id=? ORDER BY id",(aid,)).fetchall()
    def dashboard_counts(self): return {r["state"]:r["count"] for r in self.connection.execute("SELECT state,count(*) count FROM jobs GROUP BY state")}

    def portfolio_metrics(self) -> dict[str, int]:
        """Return only persisted counts for the portfolio dashboard."""
        count = lambda sql, values=(): int(self.connection.execute(sql, values).fetchone()[0])
        return {
            "discovered": count("SELECT COUNT(*) FROM jobs"),
            "eligible": count("SELECT COUNT(*) FROM job_search_eligibility WHERE eligible=1"),
            "direct_verified": count(
                "SELECT COUNT(*) FROM job_application_resolutions WHERE resolution_status='DIRECT_VERIFIED'"
            ),
            "prepared": count(
                "SELECT COUNT(*) FROM applications WHERE state IN ('MATERIALS_READY','APPROVED','READY_TO_APPLY','APPLYING','HUMAN_VERIFICATION_REQUIRED','APPLIED')"
            ),
            "applied": count("SELECT COUNT(*) FROM applications WHERE state='APPLIED'"),
            # ``APPLIED`` is reached only through the verified-success path;
            # execution sessions may later retain diagnostics from a platform
            # recovery, so they are not the durable outcome authority.
            "verified_submissions": count("SELECT COUNT(*) FROM applications WHERE state='APPLIED'"),
            "human_checkpoints": count(
                "SELECT COUNT(DISTINCT application_id) FROM application_execution_sessions WHERE state IN ('WAITING_FOR_USER','HUMAN_VERIFICATION_REQUIRED','BROWSER_CONTEXT_MISMATCH')"
            ),
            "duplicates_prevented": count(
                "SELECT COUNT(*) FROM job_duplicate_checks WHERE status IN ('DUPLICATE_CONFIRMED','ALREADY_APPLIED')"
            ),
            "evaluated": count("SELECT COUNT(*) FROM opportunity_evaluations"),
            "saved": count(
                "SELECT COUNT(DISTINCT application_id) FROM application_events WHERE reason='Saved for later'"
            ),
            "skipped": count("SELECT COUNT(*) FROM applications WHERE state='SKIPPED'"),
            "interview": count("SELECT COUNT(*) FROM applications WHERE state='INTERVIEW'"),
            "offer": count("SELECT COUNT(*) FROM applications WHERE state='OFFER'"),
        }

    def portfolio_case_state(self, company: str, title: str) -> str | None:
        row = self.connection.execute(
            "SELECT a.state FROM applications a JOIN jobs j ON j.id=a.job_id WHERE lower(j.company)=lower(?) AND lower(j.title)=lower(?) ORDER BY a.id DESC LIMIT 1",
            (company, title),
        ).fetchone()
        return row["state"] if row else None

    def portfolio_case_score(self, company: str, title: str) -> int | None:
        row = self.connection.execute(
            "SELECT m.score FROM match_assessments m JOIN jobs j ON j.id=m.job_id WHERE lower(j.company)=lower(?) AND lower(j.title)=lower(?) ORDER BY m.id DESC LIMIT 1",
            (company, title),
        ).fetchone()
        return int(row["score"]) if row else None

    def portfolio_filtered_case(self):
        return self.connection.execute(
            "SELECT j.title,j.company FROM jobs j JOIN screenings s ON s.job_id=j.id WHERE s.id=(SELECT max(id) FROM screenings WHERE job_id=j.id) AND s.eligibility_passed=0 ORDER BY j.id DESC LIMIT 1"
        ).fetchone()
    def get_score_config(self) -> ScoreConfig:
        row=self.connection.execute("SELECT value_json FROM user_settings WHERE key='score_config'").fetchone()
        if not row: return ScoreConfig()
        values=json.loads(row["value_json"])
        if "good_match_threshold" not in values:
            values["good_match_threshold"]=values.get("possible_match_threshold",70)
            values["possible_match_threshold"]=55
        return ScoreConfig(**values)
    def save_score_config(self, config: ScoreConfig) -> None:
        self.connection.execute("INSERT INTO user_settings(key,value_json,updated_at) VALUES ('score_config',?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",(json.dumps(config.__dict__),_now())); self.connection.commit()
    def get_search_config(self) -> dict:
        row=self.connection.execute("SELECT value_json FROM user_settings WHERE key='job_search_config'").fetchone()
        return json.loads(row["value_json"]) if row else {"roles":"","locations":"","work_mode":"","employment_type":"","include":"","exclude":"","level":"","greenhouse_boards":""}
    def save_search_config(self, config: dict) -> None:
        self.connection.execute("INSERT INTO user_settings(key,value_json,updated_at) VALUES ('job_search_config',?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",(json.dumps(config),_now())); self.connection.commit()
    def _event(self,aid,old,new,actor,reason): self.connection.execute("INSERT INTO application_events(application_id,from_state,to_state,actor,reason) VALUES (?,?,?,?,?)",(aid,old,new,actor,reason))
    def _execution_event(self, session_id: int, state: str, step: str, details: dict) -> None: self.connection.execute("INSERT INTO application_execution_events(session_id,state,step,details_json) VALUES (?,?,?,?)", (session_id, state, step, json.dumps(details)))
    def _audit(self,t,i,e,a,d): self.connection.execute("INSERT INTO audit_events(entity_type,entity_id,event_type,actor,details_json) VALUES (?,?,?,?,?)",(t,i,e,a,json.dumps(d,default=str)))
