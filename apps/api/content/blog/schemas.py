"""Shapes the model returns for the blog pipeline."""

from typing import Literal

from pydantic import BaseModel, Field

Level = Literal["high", "medium", "low"]


class TopicIdea(BaseModel):
    title: str
    primary_keyword: str
    secondary_keywords: list[str] = Field(max_length=4)
    intent: str
    blog_type: str
    angle: str
    why_now: str
    evidence: str
    cluster: str
    priority: Level
    overlaps: list[str] = Field(max_length=4)


class TopicIdeas(BaseModel):
    ideas: list[TopicIdea] = Field(min_length=1, max_length=20)


class PlanSection(BaseModel):
    heading: str
    purpose: str
    key_points: list[str] = Field(max_length=12)
    target_words: int
    format: str
    subheadings: list[str] = Field(max_length=8)
    direct_answer_question: str
    link_to: list[str] = Field(max_length=3)


class ImagePlan(BaseModel):
    placement: str
    concept: str
    alt: str
    caption: str


class Plan(BaseModel):
    intent: str
    audience: str
    information_gap: str
    h1: str
    seo_title: str
    meta_description: str
    slug: str
    key_takeaways: list[str] = Field(max_length=6)
    sections: list[PlanSection] = Field(min_length=3, max_length=24)
    faq_questions: list[str] = Field(min_length=10, max_length=40)
    entities: list[str] = Field(max_length=20)
    needs_source: list[str] = Field(max_length=20)
    image_plan: list[ImagePlan] = Field(max_length=8)
    schema_types: list[str] = Field(max_length=6)
    cta: str
    freshness_notes: str
    faq_heading: str = ""


class SectionOut(BaseModel):
    markdown: str
    summary: str
    open_claims: list[str] = Field(max_length=8)


class FaqItem(BaseModel):
    question: str
    answer: str


class FaqOut(BaseModel):
    items: list[FaqItem] = Field(min_length=10, max_length=40)


class CategoryScore(BaseModel):
    category: str
    score: int = Field(ge=0, le=100)
    note: str


class Weakness(BaseModel):
    severity: Level
    where: str
    fix: str


class UnsupportedClaim(BaseModel):
    quote: str
    source_needed: str


class ContentGap(BaseModel):
    covered_better: str
    competitors_often_miss: str
    added_value: str


class SchemaAdvice(BaseModel):
    type: str
    reason: str


class Verdict(BaseModel):
    value: Literal["publish", "publish_after_fixes", "do_not_publish"]
    reason: str


class AuditOut(BaseModel):
    scores: list[CategoryScore] = Field(max_length=14)
    strengths: list[str] = Field(max_length=6)
    weaknesses: list[Weakness] = Field(max_length=14)
    unsupported_claims: list[UnsupportedClaim] = Field(max_length=15)
    content_gap: ContentGap
    improvements: list[str] = Field(max_length=12)
    update_schedule: str
    structured_data: list[SchemaAdvice] = Field(max_length=6)
    verdict: Verdict


class ImagePrompt(BaseModel):
    prompt: str
    filename: str
    alt: str
    caption: str
