# Pedagogical Tutoring SFT Dataset

## Objective

Create a high-quality collection of multi-turn student-tutor conversations that demonstrates compliance with explicit pedagogical instructions while retaining a separate set of ordinary instruction-following examples.

## Required dataset components

### Expert-written golden set

Create a small, carefully reviewed set of tutoring conversations written or approved by subject-matter and pedagogy experts. This set establishes the target behavior and serves as few-shot context for synthetic data generation.

Each conversation should demonstrate:

- one instructional step at a time;
- waiting for the student's response before advancing;
- withholding an unearned final answer;
- progressive hints, beginning with the smallest useful hint;
- diagnosis and correction of student misconceptions;
- concise tutor turns focused on one idea;
- encouragement that praises effort or strategy;
- normalization of mistakes; and
- a clear stopping point once the student demonstrates understanding.

### Reviewed synthetic tutoring set

Expand the golden set with synthetic conversations generated from varied learning scenarios. Golden examples should be used as few-shot demonstrations during generation.

Synthetic examples must be manually or programmatically reviewed before acceptance. Unreviewed generations must not enter the published dataset.

### General instruction set

Include a separate collection of ordinary instruction-following conversations to preserve non-tutoring behavior. These examples should not contain pedagogical system instructions or vague system instructions added solely for consistency.

The pedagogical and general subsets must remain separately identifiable so their composition can be controlled downstream.

## Pedagogical system instructions

Every pedagogical conversation must begin with a system instruction that describes the desired teaching behavior rather than the specific problem being solved.

The instruction must distinguish:

- **Hard constraints:** requirements that must always be followed, such as not revealing the answer and proceeding one step at a time.
- **Soft constraints:** preferred style and interaction qualities, such as warmth, brevity, encouragement, and tone.

System instructions should vary in wording and pedagogical configuration while remaining precise and testable.

## Minimum record schema

Each record should contain at least:

```yaml
conversation_id: string
subset: golden | synthetic | general
messages:
  - role: system | student | tutor
    content: string
subject: string
topic: string
difficulty_level: string | null
learning_objective: string | null
scenario_type: string | null
pedagogy:
  hard_constraints: []
  soft_constraints: []
source_provenance: string
generation_metadata: object | null
review:
  status: pending | accepted | rejected
  reviewer_id: string | null
  rubric_version: string | null
  notes: string | null
```

General instruction records may leave pedagogy-specific fields empty, but they must still include provenance and review status.

## Creation workflow

1. Define the target subjects, topics, learning objectives, and student scenarios.
2. Write and review the golden conversations.
3. Define reusable system-instruction templates containing explicit hard and soft constraints.
4. Generate synthetic conversations using the golden set as demonstrations.
5. Review synthetic records for correctness, pedagogical quality, natural dialogue, and compliance with the attached instruction.
6. Acquire and validate the general instruction subset.
7. Normalize all accepted records into one versioned schema.
8. Deduplicate conversations and check for leakage across published splits.
9. Publish immutable manifests, dataset statistics, and review reports.

## Quality gates

A pedagogical record is accepted only if:

- the academic content is correct;
- the system instruction describes pedagogy rather than disclosing the solution;
- every tutor response complies with the hard constraints;
- the tutor responds appropriately to the student's latest message;
- hints progress gradually instead of jumping to the answer;
- the dialogue contains no contradictory roles or broken turn ordering;
- the conversation is natural and self-contained;
- provenance is recorded; and
- the required review is complete.

Quality should take priority over raw conversation volume. The source proposal cites approximately 100 students' worth of data as a feasibility reference, but it does not define an exact number of conversations or tokens.

## Required deliverables

- Versioned golden tutoring dataset.
- Versioned reviewed synthetic tutoring dataset.
- Versioned general instruction dataset.
- Combined dataset manifest with subset counts and composition statistics.
- System-instruction template library.
- Annotation and review rubric.
- Rejection log with standardized reason codes.
- Train, validation, and held-out manifests with leakage checks.
- Dataset card describing scope, provenance, limitations, and known biases.

## Decisions still required

- Target subjects and topic coverage.
- Intended learner levels and misconception categories.
- Required number of golden and synthetic conversations.
- Desired proportion of pedagogical and general instruction data.
- Human review staffing and acceptance thresholds.
- Split proportions and rules for grouping related conversations.
- Licensing and privacy requirements for every source.

## Source

`P7 PRD.pdf`, especially Implementation 2 on pages 7-10.
