"""
retrieval/gd_college_data.py
-----------------------------
Static "Golden Source" knowledge base for GD College
(Glamour & Distinction College, Calgary, Alberta, Canada).

WHAT THIS FILE DOES
-------------------
Defines `gd_college_raw_data`, a Python list of structured dictionaries.
Each dictionary represents one discrete piece of institutional knowledge
that the AI voice agent is permitted to present to callers.

Topics covered:
  - General institutional info (mission, location, hours, contact)
  - Academic programs (diploma details, start dates, duration, salary estimates)
  - Critical operational policies (call duration cap, sensitive-topic refusals)
  - Existing student FAQs (portal access, transcripts, graduation certificates)
  - Alumni support FAQs (employment verification, certificate reissue)

WHY IT EXISTS
-------------
The previous architecture used Pinecone — a US-hosted SaaS vector store —
which violated the project's Canadian data-residency requirement (C1).  This
file replaces that dependency entirely.

Benefits of this approach:
  1. Data residency: All knowledge stays on-premises / in the Canadian AWS region
     until the migration script explicitly pushes it to RDS ca-central-1.
  2. Version control: Every knowledge change is a tracked Git commit, enabling
     full audit history (M3 requirement).
  3. Testability: Unit tests can import this list directly without any network
     dependency.
  4. Single source of truth: The migration script (migrate_to_pgvector.py) is the
     ONLY consumer — nothing reads this file at query time, so updating it and
     re-running the migration is the complete change workflow.

HOW IT FITS IN THE SYSTEM
--------------------------
    gd_college_data.py   (this file — static source of truth)
          |
          | imported by
          v
    migrate_to_pgvector.py  (reads list, embeds text, inserts into PGVector DB)
          |
          v
    PostgreSQL / pgvector DB  (runtime retrieval store)
          |
          v
    vector_store.py           (KnowledgeBase.search() called by orchestrator)

RECORD SCHEMA
-------------
Each dict in `gd_college_raw_data` has exactly these keys:

    id  (str)
        Unique stable identifier used to trace a record through the migration
        pipeline.  The migration script stores this as `source_id` in the
        chunks table (M3 audit trail requirement).  Convention: two-letter
        prefix + underscore + category code + underscore + zero-padded number,
        e.g. "pc_gen_001".

    category  (str)
        High-level topic tag.  Used by vector_store.py's `get_threshold()`
        function to select the correct confidence gate for retrieval results.
        Values in use: 'General Info', 'Admissions', 'Fees', 'Academic',
        'Student FAQs', 'Alumni Support FAQs'.

    program_name  (str | None)
        Specific program this record relates to, or None for institution-wide
        information.  Stored in the chunks.metadata JSONB column so the
        orchestrator can filter results by program if needed.

    text  (str)
        The verbatim knowledge text that will be embedded and stored in PGVector.
        This is the content returned to the LLM during RAG retrieval — write it
        in the same register the agent would use when speaking to a caller.

    is_sensitive_topic  (bool)
        When True, the record concerns a legally or ethically sensitive area
        (e.g. immigration advice, harassment complaints).  The governance layer
        applies stricter handling: higher confidence thresholds, mandatory
        refusal language, and elevated audit logging.

    hard_refusal_category  (str | None)
        When set, the orchestrator MUST refuse to engage with this topic and
        redirect the caller.  The value maps to a threshold override in
        config/rag_thresholds.json and to a specific refusal prompt in the
        orchestrator.  Values in use:
            'HARD_REFUSAL_IMMIGRATION' — caller asks for visa / immigration advice.
            'HARD_REFUSAL_LEGAL'       — caller mentions lawsuits or harassment claims.
"""

# ---------------------------------------------------------------------------
# gd_college_raw_data
# The complete knowledge corpus for GD College.
#
# Organisation (five logical sections):
#   1. General institutional info  (pc_gen_*, pc_adm_*, pc_fee_*, pc_aca_001)
#   2. Academic programs           (pc_edu_*)
#   3. Critical policies           (pc_pol_*)
#   4. Existing student FAQs       (pc_stf_*)
#   5. Alumni support FAQs         (pc_alu_*)
# ---------------------------------------------------------------------------
gd_college_raw_data = [

    # =========================================================================
    # SECTION 1 — GENERAL INSTITUTIONAL INFO
    # These records answer the most common opener questions callers ask:
    # "What is GD College?", "Where are you?", "When are you open?",
    # "How do I apply?", "How do I pay?", "Do you have evening classes?"
    # =========================================================================

    # Mission statement — establishes the college's purpose and target audience.
    # Returned when callers ask "what does GD College do?" or "who is this for?"
    {
        "id": "pc_gen_001",           # Stable audit-trail ID (M3).
        "category": "General Info",   # Maps to threshold lookup in vector_store.py.
        "program_name": None,         # Not program-specific — applies to the whole institution.
        "text": (
            "GD College Mission: To empower students of all genders with skills for "
            "financial independence in beauty and cosmetology. We focus on business "
            "marketing, portfolio building, and job interview preparation."
        ),
        "is_sensitive_topic": False,  # No sensitive content — safe to surface freely.
        "hard_refusal_category": None # No refusal required for this topic.
    },

    # Physical address — the most frequently requested piece of information on calls.
    # Returned when callers ask "where are you located?" or "what is your address?"
    {
        "id": "pc_gen_002",
        "category": "General Info",
        "program_name": None,
        "text": (
            "GD College is located in Calgary, Alberta, Canada. "
            "The specific address is #108, 1935- 27 ave NE, Calgary, AB T2E 7E4."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # Operating hours and contact details.
    # Returned when callers ask "what time do you open?", "are you open Saturday?",
    # or "how do I reach someone from admissions?"
    # Note: Friday is closed — the agent must state this clearly to avoid wasted trips.
    {
        "id": "pc_gen_003",
        "category": "General Info",
        "program_name": None,
        "text": (
            "Official working hours: Monday - Thursday: 09:30 AM - 5:00 PM, "
            "Friday: Closed, Saturday - Sunday: 10:00 AM - 4:00 PM. "
            "Contact the admissions team at info@gdcollege.ca or phone at +1 587-349-1110."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # General admissions requirements — applies to all programs unless a program-specific
    # record overrides it.  Returned for "how do I enrol?", "do I need a high school diploma?"
    {
        "id": "pc_adm_001",
        "category": "Admissions",
        "program_name": None,
        "text": (
            "General Admissions: A high school diploma or equivalent is required. "
            "You can apply online via the GD College website or visit the campus."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # Financial aid and payment options.
    # Intentionally kept high-level so the agent does not promise specific financing
    # terms.  For precise quotes, callers should speak to the admissions team.
    {
        "id": "pc_fee_001",
        "category": "Fees",
        "program_name": None,
        "text": (
            "Financial Aid and Payment Options: Full Payment offers a 5% discount. "
            "We also offer Monthly Installments and various Student financing options. "
            "Comprehensive Study Kits are provided for programs."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # Schedule flexibility — covers the "do you have evening or weekend classes?" question
    # which is one of the top concerns for working adult callers.
    {
        "id": "pc_aca_001",
        "category": "Academic",
        "program_name": None,  # Institution-wide — not tied to any single program.
        "text": (
            "Class Schedules: We offer flexible schedules including morning, afternoon, "
            "evening, and weekend options. We feature state-of-the-art facilities, "
            "experienced faculty, and small class sizes."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # =========================================================================
    # SECTION 2 — ACADEMIC PROGRAMS
    # Each record covers one diploma / program with: duration, format (on-site),
    # next intake date, career paths (where available), and salary estimates.
    # These are the most frequently discussed topics on prospecting calls.
    # =========================================================================

    # Esthetician Diploma — the flagship entry-level program.
    # 5 months, on-site, next batch Feb 2026.
    # Salary range provided so callers understand return on investment.
    {
        "id": "pc_edu_001",
        "category": "Academic",
        "program_name": "Esthetician Diploma",  # Used for program-specific filtering.
        "text": (
            "Esthetician Diploma: A 5-month on-site program with state-of-the-art facilities. "
            "Next Batch: February 24, 2026. "
            "Career options include Spa/Salon Esthetician, Freelance Esthetician, Educator. "
            "Average Estimated Starting wage per year: $37,000 CAD, "
            "Experienced wage: $45,000 CAD."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # Clinical Esthetician Diploma — advanced medical-adjacent skincare program.
    # Higher salary ceiling reflects the clinical / therapeutic specialisation.
    # Includes job placement assistance, which is a key differentiator.
    {
        "id": "pc_edu_002",
        "category": "Academic",
        "program_name": "Clinical Esthetician",
        "text": (
            "Clinical Esthetician Diploma: A 5-month on-site professional certification. "
            "Includes job placement assistance. "
            "Next Batch: May 18, 2026. "
            "Specialize in clinical skincare treatments and advanced therapeutic procedures. "
            "Average Estimated Starting wage per year: $47,000 CAD, "
            "Experienced wage: $70,000 CAD."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # Advanced Esthetics Diploma — longer 10-month (8-month intensive) program.
    # Practical training with real clients is a key selling point for callers who
    # want hands-on industry experience before graduating.
    {
        "id": "pc_edu_003",
        "category": "Academic",
        "program_name": "Advanced Esthetics Diploma",
        "text": (
            "Advanced Esthetics Diploma: A 10-month on-site program (8-month intensive). "
            "Covers practical training with real clients. "
            "Next Batch: February 24, 2026. "
            "Average Estimated Starting wage per year: $45,000 CAD, "
            "Experienced wage: $47,000 CAD."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # Makeup Artist & Hairstylist Diploma — creative arts program, shortest duration (4 months).
    # 'No prior experience needed' is a strong inclusion signal for callers who are
    # considering a career change and feel unqualified.
    {
        "id": "pc_edu_004",
        "category": "Academic",
        "program_name": "Makeup Artist & Hairstylist Diploma",
        "text": (
            "Makeup Artist & Hairstylist Diploma: A 4-month on-site program. "
            "Requires high school diploma; no prior experience needed. "
            "Includes career counselling. "
            "Next Batch: February 24, 2026."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # Massage Therapy Diploma — the only regulated health-profession program offered.
    # 2-year duration is significantly longer than other programs; callers comparing
    # programs need this clearly communicated.
    {
        "id": "pc_edu_005",
        "category": "Academic",
        "program_name": "Massage Therapy",
        "text": (
            "Massage Therapy Diploma: A 2-year on-site professional program. "
            "Master the art of therapeutic massage and healing techniques. "
            "Next Batch: May 18, 2026."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # Nail Technician Diploma — beginner-friendly, shortest combined duration (4 months / 14 weeks).
    # 'Suitable for beginners' directly addresses the most common hesitation on niche-skill calls.
    {
        "id": "pc_edu_006",
        "category": "Academic",
        "program_name": "Nail Technician",
        "text": (
            "Nail Technician Diploma: A 4-month (14-week) on-site course. "
            "Suitable for beginners. "
            "Next Batch: February 24, 2026."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # =========================================================================
    # SECTION 3 — CRITICAL POLICIES
    # These records govern agent BEHAVIOUR, not factual answers.
    # They are marked is_sensitive_topic=True so the orchestrator applies
    # stricter handling: higher confidence thresholds, mandatory refusal
    # language, and elevated audit logging.
    # =========================================================================

    # Call duration policy — the agent MUST proactively end the call at 5 minutes
    # to ensure fair access for all callers and control per-call telephony costs.
    # is_sensitive_topic=True ensures this record is always surfaced with high
    # confidence and never dropped by the confidence gate.
    {
        "id": "pc_pol_001",
        "category": "General Info",
        "program_name": None,
        "text": (
            "CALL DURATION LIMIT: To ensure all students can be served, each automated "
            "session is restricted to a maximum of 5 minutes."
        ),
        "is_sensitive_topic": True,   # Policy enforcement record — handle with elevated care.
        "hard_refusal_category": None # No hard refusal — agent can surface this information.
    },

    # Immigration / visa policy — legally risky territory.
    # The college is NOT licensed to provide immigration advice.  Giving such advice
    # could expose the college to regulatory liability.  The agent MUST redirect the
    # caller to IRCC (Immigration, Refugees and Citizenship Canada) without any
    # substantive engagement on visa details.
    {
        "id": "pc_pol_002",
        "category": "Student FAQs",
        "program_name": None,
        "text": (
            "SENSITIVE: GD College does not provide immigration or visa advice. "
            "Students must contact IRCC directly."
        ),
        "is_sensitive_topic": True,                      # Legally sensitive — strict handling.
        "hard_refusal_category": "HARD_REFUSAL_IMMIGRATION"  # Triggers mandatory refusal in orchestrator.
    },

    # Legal / harassment policy — the agent must NEVER engage with lawsuit or legal-threat
    # language.  Doing so could constitute an admission or create legal liability.
    # All such matters must be redirected to the college's legal department immediately.
    {
        "id": "pc_pol_003",
        "category": "Student FAQs",
        "program_name": None,
        "text": (
            "SENSITIVE: We have a zero-tolerance policy for harassment. "
            "Any legal action or lawsuit should be directed to our legal department."
        ),
        "is_sensitive_topic": True,                 # Legally sensitive — strict handling.
        "hard_refusal_category": "HARD_REFUSAL_LEGAL"  # Triggers mandatory refusal in orchestrator.
    },

    # =========================================================================
    # SECTION 4 — EXISTING STUDENT FAQs
    # Information for currently enrolled students.  These questions come in
    # after enrolment and relate to day-to-day student administration.
    # =========================================================================

    # Covers three related FAQs in one record to keep the knowledge base compact:
    #   1. Student portal access (URL + credentials)
    #   2. Official transcript request process (portal tab + SLA: 3–5 business days)
    #   3. Graduation certificate timeline (4–6 weeks after final grades posted)
    # A single record is acceptable because these topics almost always co-occur
    # in student queries about "academic records".
    {
        "id": "pc_stf_001",
        "category": "Student FAQs",
        "program_name": None,  # Applies to all enrolled students across all programs.
        "text": (
            "Currently enrolled students can access the student portal at portal.gdcollege.ca "
            "using their student ID and initial password provided during orientation. "
            "Official transcript requests must be submitted through the portal under the "
            "'Academic Records' tab and take 3 to 5 business days to process. "
            "Certificate request timelines for graduation processing are typically "
            "4 to 6 weeks after final grades are posted."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # =========================================================================
    # SECTION 5 — ALUMNI SUPPORT FAQs
    # Post-graduation support queries from former students or third parties
    # (e.g. employers requesting credential verification).
    # =========================================================================

    # Covers two alumni FAQs in one record:
    #   1. Employment / education verification process for third parties
    #      (email alumni@gdcollege.ca + signed consent form from the former student).
    #   2. Lost certificate replacement procedure ($50 CAD fee + 10-business-day SLA).
    # Both require the agent to direct the caller to specific contacts / processes
    # rather than resolving the matter on the call.
    {
        "id": "pc_alu_001",
        "category": "Alumni Support FAQs",
        "program_name": None,  # Applies to all alumni regardless of program.
        "text": (
            "Alumni verification for employment or further education requires third parties "
            "to email alumni@gdcollege.ca with a signed consent form from the former student. "
            "If an alumni loses their degree certificate, the reissue process requires "
            "completing the 'Parchment Replacement Form' and paying a $50 CAD processing fee. "
            "Reissued certificates are mailed within 10 business days."
        ),
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    }
]
