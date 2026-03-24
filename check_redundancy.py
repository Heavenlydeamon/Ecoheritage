import os
import django
import sys

# Setup Django
# Get the absolute path of the current directory (d:\main project\ecoheritage)
current_dir = os.getcwd()
sys.path.append(current_dir)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecoheritage.settings')
django.setup()

from mainapp.models import Topic, StudyMaterial

def check_overlaps():
    topics = Topic.objects.all()
    print(f"Total Topics: {len(topics)}")
    
    # Check for Western Ghats specifically
    wg_topics = Topic.objects.filter(name__icontains='Ghats') | Topic.objects.filter(description__icontains='Ghats')
    print("\n[Western Ghats Related Topics]")
    for t in wg_topics:
        print(f"ID: {t.id} | Name: {t.name} | Section: {t.section.name}")
        for m in t.study_materials.all().order_by('order'):
            print(f"  - Material: {m.title} (ID: {m.id})")

    # Check for Physical Geography overlap
    phys_topics = Topic.objects.filter(name__icontains='Physical')
    print("\n[Physical Geography Related Topics]")
    for t in phys_topics:
        print(f"ID: {t.id} | Name: {t.name} | Section: {t.section.name}")
        for m in t.study_materials.all().order_by('order'):
            print(f"  - Material: {m.title} (ID: {m.id})")

    # Check for potential duplicates by content start
    print("\n[Potentially Redundant Materials (First 50 chars match)]")
    materials = list(StudyMaterial.objects.all())
    # Group by first 50 characters
    content_map = {}
    for m in materials:
        start = m.content_text[:60].strip().lower()
        if len(start) < 20: continue
        if start not in content_map:
            content_map[start] = []
        content_map[start].append(m)
    
    for start, ms in content_map.items():
        if len(ms) > 1:
            print(f"Potential Duplicate Set (Start: '{start}...')")
            for m in ms:
                print(f"  - ID {m.id} | Topic: {m.topic.name} | Title: {m.title}")

if __name__ == "__main__":
    check_overlaps()
