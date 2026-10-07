"""
Module: quiz.py

Description:
    The developer quiz as the model writes it and the server stores it: `Quiz`, a title and its
    multiple-choice `Question`s (or none, for a cosmetic change). The model's reply is validated
    against these models, and the answer keys (`correct`) never leave the server.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Question(BaseModel):
    """One multiple-choice question; `correct` indexes `options`."""
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=10, max_length=1000)
    options: list[str] = Field(min_length=4, max_length=4)
    correct: int = Field(ge=0, le=3)
    explanation: str = Field(min_length=5, max_length=2000)

    @model_validator(mode="after")
    def unique_options(self) -> "Question":
        """
        Reject empty, oversized or duplicate options.
        Returns:
            Question: The validated question.
        Raises:
            ValueError: If an option is empty or too long, or two options match.
        """
        if any(not x.strip() or len(x) > 1000 for x in self.options):
            raise ValueError("Options must be nonempty and at most 1000 characters")
        if len(set(x.strip().casefold() for x in self.options)) != 4:
            raise ValueError("Options must be distinct")
        return self


class Quiz(BaseModel):
    """
    A quiz as the model writes it: a title and 3 to 5 questions, or, for a change that only touches
    comments, formatting or documentation, `cosmetic` set and no questions.
    """
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=3, max_length=200)
    cosmetic: bool = False
    questions: list[Question] = Field(max_length=5)

    @model_validator(mode="after")
    def questions_match_kind(self) -> "Quiz":
        """
        Require 3 to 5 questions for a code change, and none for a cosmetic one.
        Returns:
            Quiz: The validated quiz.
        Raises:
            ValueError: If the number of questions does not match `cosmetic`.
        """
        if self.cosmetic and self.questions:
            raise ValueError("A cosmetic change has no questions")
        if not self.cosmetic and len(self.questions) < 3:
            raise ValueError("A code change needs at least 3 questions")
        return self
