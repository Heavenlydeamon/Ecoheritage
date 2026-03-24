import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ecoheritage.settings')
django.setup()

from mainapp.models import CollectibleItem, League, ArtifactShard

def seed_gamification():
    print("Seeding Gamification Data...")
    
    # 1. Seed Leagues
    leagues = [
        ('The Outsiders', 0, 100, 'fa-person-hiking'),
        ('Periyar League', 101, 500, 'fa-water'),
        ('Silent Valley Elite', 501, 1000, 'fa-leaf'),
        ('Ghats Immortals', 1001, 5000, 'fa-mountain-sun'),
    ]
    for name, min_xp, max_xp, icon in leagues:
        League.objects.get_or_create(name=name, defaults={'min_xp': min_xp, 'max_xp': max_xp, 'icon': icon})
    
    # 2. Seed Collectibles
    collectibles = [
        ('Neelakurinji', 'flora', 'The flower that blooms once in 12 years.', 'rare', 100),
        ('Malabar Giant Squirrel', 'fauna', 'The colorful acrobat of the canopy.', 'epic', 200),
        ('Nilgiri Tahr', 'fauna', 'The mountain monarch of Eravikulam.', 'legendary', 500),
        ('Theyyam Mask', 'folklore', 'The divine dance of the spirits.', 'rare', 150),
        ('Kalaripayattu Shield', 'artifact', 'The ancient defense of the Malabar warriors.', 'epic', 300),
    ]
    for name, category, desc, rarity, xp in collectibles:
        CollectibleItem.objects.get_or_create(name=name, defaults={'category': category, 'description': desc, 'rarity': rarity, 'xp_value': xp})
        
    # 3. Seed Artifact Shards
    shards = [
        ('Edakkal Shard A', 'Prehistoric engravings.', 1),
        ('Edakkal Shard B', 'Caves of history.', 2),
        ('Edakkal Shard C', 'Ancient script.', 3),
        ('Edakkal Shard D', 'The complete record.', 4),
    ]
    # We need an artifact item for the shards
    artifact_item, _ = CollectibleItem.objects.get_or_create(
        name='Edakkal Script', 
        defaults={'category': 'artifact', 'description': 'The complete prehistoric record.', 'rarity': 'epic', 'xp_value': 1000}
    )
    
    for name, desc, index in shards:
        ArtifactShard.objects.get_or_create(name=name, defaults={'description': desc, 'parent_artifact': artifact_item, 'shard_index': index, 'total_shards': 4})

    print("Seeding Complete!")

if __name__ == "__main__":
    seed_gamification()
