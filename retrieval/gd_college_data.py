gd_college_raw_data = [
    # --- 1. ADMISSIONS & PROGRAMS ---
    {
        "category": "Admissions",
        "program_name": "Diploma in Computer Science",
        "text": "The Diploma in Computer Science is a 2-year full-time program. Eligibility requires a high school diploma with a minimum of 65% in Grade 12 Mathematics and English. Admission steps include submitting the online application, uploading official transcripts, and paying the application fee. Intake periods are Fall (September) and Winter (January). The program is delivered in-person at the Toronto Metro Campus.",
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },
    {
        "category": "Admissions",
        "program_name": "Post-Graduate Certificate in Business Management",
        "text": "The Post-Graduate Certificate in Business Management is a 1-year program available both online and offline. Eligibility requires a recognized bachelor's degree. Required documents include a resume, statement of purpose, and university transcripts. The primary intake is Fall (September).",
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # --- 2. FEES (Static Only) ---
    {
        "category": "Fees",
        "program_name": "Diploma in Computer Science",
        "text": "The tuition fee for the Diploma in Computer Science is $16,000 CAD per academic year for international students and $4,500 CAD for domestic students. A non-refundable application fee of $150 CAD applies to all applicants. Payment for the first semester is due 30 days prior to the start date. GD College offers merit-based entrance scholarships ranging from $500 to $2000 CAD.",
        "is_sensitive_topic": True, # Flagged as sensitive so the LLM handles it carefully
        "hard_refusal_category": None
    },

    # --- 3. ACADEMIC INFORMATION ---
    {
        "category": "Academic", # Mapped from 'Academic Information'
        "program_name": None,
        "text": "Standard diploma courses have a duration of 4 semesters (2 years). Upon successful completion, students are awarded an Ontario College Diploma. Attendance requirements mandate a minimum of 80% attendance in all core modules to be eligible for final examinations. Exams consist of mid-terms in week 7 and final exams in week 14.",
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # --- 4. GENERAL INSTITUTIONAL INFO ---
    {
        "category": "General Info", # Mapped from 'General Institutional Info'
        "program_name": None,
        "text": "GD College Canada is a designated learning institution (DLI) fully accredited by the provincial Ministry of Colleges and Universities. The main campus address is 100 University Avenue, Toronto, ON, M5J 1V6. Official working hours for the administrative office are Monday to Friday, 8:30 AM to 4:30 PM EST. Contact the admissions team via email at admissions@gdcollege.ca or phone at +1-416-555-0199.",
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # --- 5. EXISTING STUDENT FAQS ---
    {
        "category": "Student FAQs", # Mapped from 'Existing Student FAQs'
        "program_name": None,
        "text": "Currently enrolled students can access the student portal at portal.gdcollege.ca using their student ID and initial password provided during orientation. Official transcript requests must be submitted through the portal under the 'Academic Records' tab and take 3 to 5 business days to process. Certificate request timelines for graduation processing are typically 4 to 6 weeks after final grades are posted.",
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    },

    # --- 6. ALUMNI SUPPORT FAQS ---
    {
        "category": "Alumni Support FAQs", # Matches Alumni FAQs enum mostly
        "program_name": None,
        "text": "Alumni verification for employment or further education requires third parties to email alumni@gdcollege.ca with a signed consent form from the former student. If an alumni loses their degree certificate, the reissue process requires completing the 'Parchment Replacement Form' and paying a $50 CAD processing fee. Reissued certificates are mailed within 10 business days.",
        "is_sensitive_topic": False,
        "hard_refusal_category": None
    }
]
