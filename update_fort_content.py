import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecoheritage.settings')
django.setup()

from mainapp.models import Topic, StudyMaterial

t = Topic.objects.filter(name__icontains='Fort Kochi').first()

if t:
    print(f"Updating topic: {t.name}")
    # Update topic description
    t.description = "Fort Kochi is not merely a town; it is a living museum. As the first European township in India, it served as the primary gateway for the global spice trade, weaving together a 500-year-old tapestry of Portuguese, Dutch, British, and Arab influences into the local fabric of Kerala."
    t.save()

    StudyMaterial.objects.filter(topic=t).delete()

    StudyMaterial.objects.create(topic=t, title='Historical Genesis', content_text='''Fort Kochi is a coastal palimpsest where every street corner whispers a different century. Nestled on the reclaimed islands of the Kochi Lake, it remains one of the few places in the world where a Jewish Synagogue, a Dutch Palace, a Portuguese Church, and Chinese fishing nets exist within a three-kilometer radius.

**Key Highlights:**
* **Colonial Echoes:** It houses St. Francis Church, the original burial site of explorer Vasco da Gama, and the Paradesi Synagogue, the oldest active synagogue in the Commonwealth.
* **Iconic Vistas:** The shoreline is defined by the Cheenavalai—massive cantilevered Chinese fishing nets introduced by traders from the court of Kublai Khan.
* **Artistic Renaissance:** Since 2012, the area has transformed into a global canvas as the primary host of the Kochi-Muziris Biennale, India’s largest contemporary art festival.''', order=1)

    StudyMaterial.objects.create(topic=t, title='Architecture Highlights', content_text='''The architecture of Fort Kochi is a "Tropical Colonial" hybrid. European layouts (steep tiled roofs, wooden shutters) were adapted using local materials like laterite stone and teakwood to survive Kerala’s heavy monsoons.

| Year | Milestone | Architectural/Political Impact |
|---|---|---|
| 1503 | Portuguese Arrival | Built Fort Emmanuel, the first European fort in India. |
| 1663 | Dutch Conquest | Modernized the town; built the "Dutch Palace" (Mattancherry Palace). |
| 1795 | British Era | Fort Kochi became a municipality; focus shifted to commercial tea/spice exports. |
| 1947 | Post-Independence | Preservation era; evolved into a major global heritage destination. |''', order=2)

    StudyMaterial.objects.create(topic=t, title='Folklore & Social Impact', content_text='''The social fabric of Fort Kochi is defined by pluralism.
* **The Spice Legacy:** Local folklore tells of the "Black Gold" (pepper) that lured explorers.
* **Cuisine:** The famous "Meen Pollichathu" (fish in banana leaf) and Kochi’s unique bakery culture are direct results of European-Malayali fusion.
* **The Cochin Jews:** Legends of the "Copper Plates" given by local kings to the Jewish community highlight a history of religious tolerance rarely seen elsewhere.''', order=3)

    StudyMaterial.objects.create(topic=t, title='Collection Details', content_text='''* **Cheenavalai:** From Cheena (China) and Valai (Net). These operate on a complex system of weights and pulleys, requiring at least four fishermen to balance the structure.
* **Laterite Construction:** Most colonial buildings here use red laterite bricks, which naturally cool the interiors—a primitive yet effective form of climate control.
* **Bazaar Road:** The historical "Wall Street" of Kochi, where the air still smells of ginger, turmeric, and cardamom being traded in bulk.''', order=4)

    StudyMaterial.objects.create(topic=t, title='Summary', content_text='''Fort Kochi is the architectural embodiment of Kerala’s "Global-Local" identity. It proves that heritage is not just about old buildings, but about the survival of diverse traditions—from the way fish is caught to the way art is celebrated.''', order=5)

    print("Success: Fort Kochi content fully extended and formatted in Markdown.")
else:
    print("Error: Fort Kochi topic not found!")
