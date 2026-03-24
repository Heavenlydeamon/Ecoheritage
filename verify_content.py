
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecoheritage.settings')
django.setup()

from mainapp.models import Topic, StudyMaterial

topics = Topic.objects.all()
print(f"Total topics: {topics.count()}")

target_topics = ['Kathakali', 'Theyyam']

# Check Environment topics
env_topics = Topic.objects.filter(section__name='Environment')
print(f"\n--- Environment Topics ({env_topics.count()}) ---")
for topic in env_topics:
    print(f"[{topic.id}] {topic.name} (Parent: {topic.parent_topic})")
    
# Check Cultural topics for verification of previous work
cultural_topics = Topic.objects.filter(section__name='Cultural Artforms', name__in=['Kathakali', 'Theyyam'])
for topic in cultural_topics:
    print(f"\n--- {topic.name} ---")
    material = topic.study_materials.filter(title='Introduction & Overview').first()
    if material:
        print(f"[{material.order}] {material.title}")
        print(f"    {material.content_text[:30]}...")
    else:
        print("No Introduction found!")

# The original loop for target_topics is removed as per the instruction's replacement.
# The new code block effectively replaces the previous iteration over target_topics.
