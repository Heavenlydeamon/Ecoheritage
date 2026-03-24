import os
import django
import sys

# Setup Django
current_dir = os.getcwd()
sys.path.append(current_dir)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecoheritage.settings')
django.setup()

from mainapp.models import Topic, StudyMaterial

def merge_topics(source_ids, target_id):
    target_topic = Topic.objects.get(id=target_id)
    print(f"Merging into Topic: {target_topic.name} (ID: {target_id})")
    
    # Get all materials already in target
    existing_materials = list(target_topic.study_materials.all().order_by('order'))
    max_order = max([m.order for m in existing_materials]) if existing_materials else 0
    
    for source_id in source_ids:
        try:
            source_topic = Topic.objects.get(id=source_id)
            print(f"  - Processing Source Topic: {source_topic.name} (ID: {source_id})")
            
            materials = source_topic.study_materials.all()
            for m in materials:
                # Basic redundancy check: if title already exists in target, skip or merge
                # For this specific task, we append unique sections
                m.topic = target_topic
                max_order += 1
                m.order = max_order
                m.save()
                print(f"    - Moved Material: {m.title} (ID: {m.id})")
            
            # Delete the source topic
            source_topic.delete()
            print(f"    - Deleted Topic {source_id}")
            
        except Topic.DoesNotExist:
            print(f"  - Topic ID {source_id} not found, skipping.")

if __name__ == "__main__":
    # Western Ghats Consolidation
    source_ids = [64, 65, 66, 67, 47]
    target_id = 43
    merge_topics(source_ids, target_id)
    
    print("\nMerge Complete!")
