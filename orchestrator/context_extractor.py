import re
import logging
import json
import os
from contracts.schemas import CallContext

logger = logging.getLogger("ContextExtractor")

class ContextManager:
    """
    Deterministic logic for extracting and managing call context.
    (Story S4-9: Context Memory)
    """
    
    # --- KNOWLEDGE CONSTANTS (Should be driven by config/DB later) ---
    # --- KNOWLEDGE CONSTANTS (Loaded from Config) ---
    PROGRAMS = {}
    INTAKES = {}
    YEARS = []
    MODES = {}
    CAMPUSES = {}

    def __init__(self, config_path="config/college_data.json"):
        self._load_config(config_path)

    def _load_config(self, path):
        try:
            if os.path.exists(path):
                with open(path, "r") as f:
                    data = json.load(f)
                    self.PROGRAMS = data.get("programs", {})
                    self.INTAKES = data.get("intakes", {})
                    self.YEARS = data.get("years", [])
                    self.MODES = data.get("modes", {})
                    self.CAMPUSES = data.get("campuses", {})
                logger.info(f"Loaded Context Config from {path}")
            else:
                logger.warning(f"Config file {path} not found. using defaults.")
                # Fallback purely for safety if file missing
                self.YEARS = ["2025", "2026"] 
        except Exception as e:
            logger.error(f"Failed to load context config: {e}")

    def update_context(self, context: CallContext, user_text: str, intent: str) -> dict:
        """
        Scans user text and updates the context object if clear entities are found.
        Returns a dict of changed fields (empty if no changes).
        """
        changes = {}
        lower_text = user_text.lower()
        
        # 1. Update Intents History
        context.last_intents.append(intent)
        if len(context.last_intents) > 5:
            context.last_intents.pop(0) # Keep last 5
            
        # 2. Extract Program Interest
        detected_program = self._extract_program(lower_text)
        if detected_program:
            if context.program_interest != detected_program:
                changes["program_interest"] = detected_program
                logger.info(f"Context Update: Program {context.program_interest} -> {detected_program}")
                context.program_interest = detected_program
            
        # 3. Extract Intake / Term
        term_change = self._extract_intake(lower_text, context)
        if term_change:
            changes["intake"] = context.intake
        
        # 4. Extract Name (Simple Heuristic)
        name = self._extract_name(user_text)
        if name and context.user_name != name:
            changes["user_name"] = name
            context.user_name = name
            
        # 5. Extract Mode
        mode = self._extract_mode(lower_text)
        if mode and context.study_mode != mode:
            changes["study_mode"] = mode
            context.study_mode = mode
            
        # 6. Extract Campus
        campus = self._extract_campus(lower_text)
        if campus and context.campus != campus:
            changes["campus"] = campus
            context.campus = campus
            
        return changes

    def _extract_program(self, text: str) -> str | None:
        for program_key, keywords in self.PROGRAMS.items():
            for kw in keywords:
                if re.search(r'\b' + re.escape(kw) + r'\b', text, re.IGNORECASE):
                    return program_key.title() # Return "Nursing"
        return None

    def _extract_intake(self, text: str, context: CallContext) -> bool:
        # Check Month/Season
        found_season = None
        current = context.intake or ""
        original = current
        
        for season, keywords in self.INTAKES.items():
            for kw in keywords:
                if re.search(r'\b' + re.escape(kw) + r'\b', text, re.IGNORECASE):
                    found_season = season.title()
                    break
        
        # Check Year
        found_year = None
        for year in self.YEARS:
            if re.search(r'\b' + re.escape(year) + r'\b', text, re.IGNORECASE):
                found_year = year
                break
                
        # Base pieces
        final_season = found_season
        if not final_season:
            # Try to grab existing season from current
            for s in self.INTAKES.keys():
                if s.title() in current:
                    final_season = s.title()
                    break
                    
        final_year = found_year
        if not final_year:
            # Try to grab existing year from current
            for y in self.YEARS:
                if y in current:
                    final_year = y
                    break
                    
        # Reconstruct intake safely
        components = []
        if final_season:
            components.append(final_season)
        if final_year:
            components.append(final_year)
            
        new_intake = " ".join(components)
        
        if new_intake and new_intake != current:
             context.intake = new_intake
             return True
             
        return False

    def _extract_name(self, text: str) -> str | None:
        # Words that are never names
        FALSE_POSITIVES = {
            "interested", "calling", "asking", "wondering", "student",
            "not", "also", "just", "here", "actually", "trying", "looking",
            "speaking", "calling", "going", "doing", "trying", "fine", "good",
            "okay", "yes", "no", "hello", "hi", "hey"
        }

        # Pattern 1: Explicit intro phrases — highest confidence
        intro_patterns = [
            r"my name(?:'s| is) ([a-zA-Z]+(?:\s+[a-zA-Z]+)?)",
            r"i(?:'m| am) ([a-zA-Z]+(?:\s+[a-zA-Z]+)?)",
            r"this is ([a-zA-Z]+(?:\s+[a-zA-Z]+)?) speaking",
            r"call(?:ing)? me ([a-zA-Z]+(?:\s+[a-zA-Z]+)?)",
            r"name(?:'s| is) ([a-zA-Z]+(?:\s+[a-zA-Z]+)?)",
        ]
        for p in intro_patterns:
            match = re.search(p, text, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                first_word = name.split()[0].lower()
                if first_word not in FALSE_POSITIVES:
                    return name.title()

        # Pattern 2: Short bare response (1-2 capitalised words, no other content)
        # Catches: "Akansha Kumar", "John" when agent asked "What's your name?"
        stripped = text.strip().rstrip(".,!?")
        bare_words = stripped.split()
        if 1 <= len(bare_words) <= 2 and all(w.isalpha() for w in bare_words):
            first_word = bare_words[0].lower()
            if first_word not in FALSE_POSITIVES:
                # Only treat as a name if it looks capitalised in original text (proper noun)
                if bare_words[0][0].isupper():
                    return stripped.title()

        return None

    def _extract_mode(self, text: str) -> str | None:
        for mode_key, keywords in self.MODES.items():
            for kw in keywords:
                if kw in text:
                    return mode_key.title()
        return None

    def _extract_campus(self, text: str) -> str | None:
        for campus_key, keywords in self.CAMPUSES.items():
            for kw in keywords:
                if kw in text:
                    return campus_key.title()
        return None
