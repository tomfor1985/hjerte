"""Use the same exact passage evidence when writing MCQs from mapped PDFs."""
from .generation import DraftQuestion, StrictModel
from .mapping import PassageCitation, InventoryReference
from django.core.exceptions import ValidationError


class CitedQuestion(DraftQuestion):
    references: list[PassageCitation]


class CitedQuestionBatch(StrictModel):
    questions: list[CitedQuestion]


def question_references(citations, parts):
    available={passage['id']:(part,passage) for part in parts for passage in part['passages']}
    result=[]
    seen=set()
    for citation in citations:
        if citation.passage_id not in available:
            raise ValidationError('The question cites an unknown or unsupplied primary passage.')
        if citation.passage_id in seen:
            continue
        seen.add(citation.passage_id)
        part,passage=available[citation.passage_id]
        result.append(InventoryReference(page_id=part['page_id'],section=citation.section,
            quote=passage['text'],reading_sha256=part.get('reading_sha256'),
            passage_start=passage['start'],passage_end=passage['end']).model_dump())
    if not result:
        raise ValidationError('The question needs a supplied primary passage.')
    return result
