import os
import django
import sys

# Setup Django
current_dir = os.getcwd()
sys.path.append(current_dir)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecoheritage.settings')
django.setup()

from mainapp.models import Topic, StudyMaterial

def merge_topics_mapping(mapping):
    """
    mapping: dict of {target_id: [source_ids]}
    """
    for target_id, source_ids in mapping.items():
        try:
            target_topic = Topic.objects.get(id=target_id)
            print(f"Merging into Topic: {target_topic.name} (ID: {target_id})")
            
            # Get existing materials to find max order
            existing_materials = list(target_topic.study_materials.all().order_by('order'))
            max_order = max([m.order for m in existing_materials]) if existing_materials else 0
            
            for source_id in source_ids:
                try:
                    source_topic = Topic.objects.get(id=source_id)
                    print(f"  - Processing Source Topic: {source_topic.name} (ID: {source_id})")
                    
                    materials = source_topic.study_materials.all()
                    for m in materials:
                        # Reassign to target
                        m.topic = target_topic
                        max_order += 1
                        m.order = max_order
                        m.save()
                        print(f"    - Moved Material: {m.title} (ID: {m.id})")
                    
                    # Delete SOURCE topic
                    source_topic.delete()
                    print(f"    - Deleted Topic {source_id}")
                    
                except Topic.DoesNotExist:
                    print(f"  - Source Topic ID {source_id} not found, skipping.")
            
            # Reorder all materials in target
            # Standard order: Overview (if exists) -> Significance -> Others -> Summary
            all_m = list(target_topic.study_materials.all())
            
            # Logic for reordering:
            # 1. Title contains 'Overview' or starts with Overview
            # 2. Title contains 'Significance'
            # 3. Everything else
            # 4. Title contains 'Summary'
            
            def sort_key(material):
                t = material.title.lower()
                if 'overview' in t: return (0, material.id)
                if 'significance' in t: return (1, material.id)
                if 'history' in t: return (2, material.id)
                if 'architecture' in t: return (3, material.id)
                if 'fusion' in t: return (4, material.id)
                if 'summary' in t: return (100, material.id)
                return (10, material.id)

            all_m.sort(key=sort_key)
            for i, m in enumerate(all_m):
                m.order = i + 1
                m.save()
                
        except Topic.DoesNotExist:
            print(f"Target Topic ID {target_id} not found, skipping.")

if __name__ == "__main__":
    # Heritage Mapping
    # Fort Kochi (48) <- 73, 74
    # Padmanabhapuram Palace (49) <- 75, 76
    heritage_mapping = {
        48: [73, 74],
        49: [75, 76]
    }
    
    # Cultural Mapping
    # Kathakali (55) <- 77, 78
    # Mohiniyattam (56) <- 79, 80
    # Theyyam (57) <- 81, 82
    # Kalaripayattu (58) <- 83, 84
    # Thullal (59) <- 85, 86
    # Panchavadyam (60) <- 87, 88
    # Kerala Mural Art (61) <- 89, 90
    cultural_mapping = {
        55: [77, 78],
        56: [79, 80],
        57: [81, 82],
        58: [83, 84],
        59: [85, 86],
        60: [87, 88],
        61: [89, 90]
    }
    
    print("--- Starting Heritage Merge ---")
    merge_topics_mapping(heritage_mapping)
    
    print("\n--- Starting Cultural Merge ---")
    merge_topics_mapping(cultural_mapping)
    
    print("\nGlobal Merge Complete!")
