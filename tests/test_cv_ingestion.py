from io import BytesIO

import pytest
from docx import Document

from job_agent.domain.models import CANDIDATE_FACT_TYPES, CandidateFact
from job_agent.llm.cv_extraction import cv_fact_schema, validate_facts
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.cv_parser import CVParseError, parse_cv
from job_agent.services.cv_storage import replace_canonical_pdf, store_upload
from job_agent.services.materials import grounded_materials


def test_docx_and_unsupported_parsing():
    document=Document(); document.add_paragraph("Ada Lovelace\nPython"); raw=BytesIO(); document.save(raw)
    assert "Ada Lovelace" in parse_cv("cv.docx",raw.getvalue())
    with pytest.raises(CVParseError): parse_cv("cv.txt",b"text")
    with pytest.raises(CVParseError): parse_cv("bad.pdf",b"not a pdf")


def test_text_bearing_pdf_parsing_and_uuid_storage(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer=PdfWriter(); page=writer.add_blank_page(width=612,height=792)
    stream=DecodedStreamObject(); stream.set_data(b"BT /F1 12 Tf 72 720 Td (Ada CV Python) Tj ET")
    page[NameObject("/Contents")]=stream
    page[NameObject("/Resources")]=DictionaryObject({NameObject("/Font"):DictionaryObject({NameObject("/F1"):DictionaryObject({NameObject("/Type"):NameObject("/Font"),NameObject("/Subtype"):NameObject("/Type1"),NameObject("/BaseFont"):NameObject("/Helvetica")})})})
    raw=BytesIO(); writer.write(raw)
    assert "Ada CV Python" in parse_cv("cv.pdf",raw.getvalue())
    first=store_upload(tmp_path,"cv.pdf",b"one"); second=store_upload(tmp_path,"cv.pdf",b"two")
    assert first != second and first.read_bytes()==b"one" and second.read_bytes()==b"two"


def test_replacing_the_canonical_pdf_keeps_the_canonical_path_stable(tmp_path):
    target = replace_canonical_pdf(tmp_path, b"updated-cv")
    assert target == tmp_path / "candidate_documents" / "canonical_cv.pdf"
    assert target.read_bytes() == b"updated-cv"


def test_schema_and_pending_lifecycle(tmp_path):
    facts=validate_facts({"facts":[{"fact_type":"skill","value":"Python","evidence":"Skills","snippet":"Python","confidence":"high"}]})
    with pytest.raises((ValueError, TypeError)): validate_facts({"facts":[{"fact_type":"invented"}]})
    db=connect(tmp_path/"x.db"); migrate(db); repo=JobRepository(db); profile=repo.get_or_create_profile("Ada"); doc=repo.add_document("cv.docx","data/artifacts/a.docx","application/docx","Python")
    fid=repo.add_pending_fact(profile,CandidateFact("skill",facts[0].value,facts[0].evidence,False),doc,"Python","high")
    assert repo.add_pending_fact(profile,CandidateFact("skill","Python","Skills",False),doc,"Python","high")==fid
    assert "Python" not in grounded_materials("Role","Co",repo.facts())["cover_letter"][0]
    repo.review_fact(fid,"APPROVED","Python 3","Skills")
    assert "Python 3" in grounded_materials("Role","Co",repo.facts())["cover_letter"][0]
    repo.review_fact(fid,"REJECTED")
    assert "Python 3" not in grounded_materials("Role","Co",repo.facts())["cover_letter"][0]
    assert repo.connection.execute("SELECT count(*) FROM profile_fact_versions WHERE fact_id=?",(fid,)).fetchone()[0] == 3


def test_fact_type_schema_and_validator_share_canonical_vocabulary():
    assert cv_fact_schema()["properties"]["facts"]["items"]["properties"]["fact_type"]["enum"] == list(CANDIDATE_FACT_TYPES)
    assert validate_facts({"facts":[{"fact_type":"skill","value":"Python","evidence":"CV","snippet":"Python","confidence":"high"}]})
    with pytest.raises(ValueError):
        validate_facts({"facts":[{"fact_type":"tools","value":"Python","evidence":"CV","snippet":"Python","confidence":"high"}]})
