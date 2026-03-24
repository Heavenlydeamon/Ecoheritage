import logging
import traceback
import os
import sys
import django

# Set up Django
sys.path.append(r'd:\main project\ecoheritage')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecoheritage.settings')
django.setup()

from mainapp.ai_quiz_generator import AIQuizGenerator
from mainapp.ai_engine import AIEngine

logging.basicConfig(level=logging.INFO)

sample_text = """
Kathakali is a major form of classical Indian dance. 
It is a "story play" genre of art, but one distinguished by the nearly elaborately colorful make-up, costumes and face masks that the traditionally male actor-dancers wear. 
Kathakali is a Hindu performance art in the Malayalam-speaking southwestern region of Kerala.
The traditional themes of the Kathakali are folk mythologies, religious legends and spiritual ideas from the Hindu epics and the Puranas.
The vocal part of Kathakali performance is traditionally performed in Sanskritised Malayalam.
It has its roots in many previous performing arts like Koodiyattam and Krishnanattam.
"""

def main():
    try:
        generator = AIQuizGenerator()
        print('Attempting generation...')
        # Clear cache first to force generation
        AIEngine._prompt_cache = {}
        
        # Test validation separately
        generator.validate_input(sample_text)
        
        # Test generation
        questions = generator.generate_questions(sample_text, num_questions=1)
        print(f'Generated {len(questions)} questions.')
        
        for q in questions:
            print(f'Q: {q.question_text}')
            print(f'A: {q.correct_answer}')
            
    except Exception:
        print("\n--- ERROR IN PIPELINE ---")
        traceback.print_exc()

if __name__ == "__main__":
    main()
