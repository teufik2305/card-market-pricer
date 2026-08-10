#!/usr/bin/env python3
import re
import subprocess

def create_buyer_requirements():
    """Create list of all buyer requirements"""
    requirements = {
        # Monsters/Fusion
        'Elemental-HERO-Absolute-Zero': 1,
        'Elemental-HERO-Bladedge': 1,
        'Elemental-HERO-Bubbleman': 1,
        'Elemental-HERO-Darkbright': 1,
        'Elemental-HERO-Electrum': 1,
        'Elemental-HERO-Flame-Wingman': 1,
        'Elemental-HERO-Great-Tornado': 1,
        'Elemental-HERO-Mariner': 1,
        'Elemental-HERO-Mudballman': 1,
        'Elemental-HERO-Necroid-Shaman': 1,
        'Elemental-HERO-Necroshade': 1,
        'Elemental-HERO-Nova-Master': 1,
        'Elemental-HERO-Ocean': 1,
        'Elemental-HERO-Phoenix-Enforcer': 1,
        'Elemental-HERO-Plasma-Vice': 1,
        'Elemental-HERO-Prisma': 1,
        'Elemental-HERO-Rampart-Blaster': 1,
        'Elemental-HERO-Steam-Healer': 1,
        'Elemental-HERO-Tempest': 1,
        'Elemental-HERO-The-Shining': 1,
        'Elemental-HERO-Thunder-Giant': 1,
        'Elemental-HERO-Wild-Wingman': 1,
        'Elemental-HERO-Wildedge': 1,
        'Hero-Kid': 3,
        'King-of-the-Swamp': 2,
        'Winged-Kuriboh': 1,
        'Wroughtweiler': 1,
        
        # Spells
        'A-Hero-Lives': 1,
        'Dark-Factory-of-Mass-Production': 1,
        'E-Emergency-Call': 1,
        'Fake-Hero': 1,
        'Fusion-Gate': 1,
        'Fusion-Recovery': 1,
        'Graceful-Charity': 2,
        'H-Heated-Heart': 1,
        'Harpies-Feather-Duster': 2,  # Assuming this is what "Harpie's Feather" refers to
        'Hero-Bond': 1,
        'Miracle-Fusion': 1,
        'Mirage-of-Nightmare': 1,
        'Monster-Reborn': 1,
        'O-Oversoul': 1,
        'Pot-of-Greed': 2,
        'Raigeki': 2,
        'R-Righteous-Justice': 1,
        'Silent-Doom': 1,
        'Skyscraper': 1,
        'The-Warrior-Returning-Alive': 1,
        
        # Traps
        'Draining-Shield': 1,
        'Magic-Cylinder': 1,
        'Mirror-Force': 2,
    }
    return requirements

def search_card_quantities(card_name):
    """Search for all instances of a card and return quantities"""
    try:
        # Use grep to find all instances of the card
        result = subprocess.run([
            'grep', '-A', '4', f'"{card_name}":', 'data/YuGiOh/cardlist.json'
        ], capture_output=True, text=True)
        
        if result.returncode != 0:
            return []
        
        lines = result.stdout.split('\n')
        card_instances = []
        
        i = 0
        while i < len(lines):
            if f'"{card_name}":' in lines[i]:
                # Look for quantity in the next few lines
                for j in range(1, 5):
                    if i + j < len(lines) and '"quantity":' in lines[i + j]:
                        qty_match = re.search(r'"quantity":\s*(\d+)', lines[i + j])
                        if qty_match:
                            quantity = int(qty_match.group(1))
                            if quantity > 0:
                                card_instances.append({
                                    'card_name': card_name,
                                    'quantity': quantity,
                                    'context': lines[i:i+5]
                                })
                        break
            i += 1
            
        return card_instances
        
    except Exception as e:
        print(f"Error searching for {card_name}: {e}")
        return []

def analyze_all_requirements():
    """Analyze all buyer requirements"""
    requirements = create_buyer_requirements()
    
    print("=== COMPREHENSIVE BUYER REQUIREMENTS ANALYSIS ===\n")
    print(f"Analyzing {len(requirements)} required cards...\n")
    
    available_cards = {}
    missing_cards = {}
    
    for card_name, needed_qty in requirements.items():
        print(f"Searching for: {card_name}")
        instances = search_card_quantities(card_name)
        
        if instances:
            total_available = sum(instance['quantity'] for instance in instances)
            available_cards[card_name] = {
                'needed': needed_qty,
                'available': total_available,
                'sufficient': total_available >= needed_qty,
                'instances': instances
            }
            status = "✅ SUFFICIENT" if total_available >= needed_qty else "⚠️  INSUFFICIENT"
            print(f"  Found {len(instances)} instances, total quantity: {total_available} - {status}")
        else:
            missing_cards[card_name] = {
                'needed': needed_qty,
                'available': 0
            }
            print(f"  ❌ NOT FOUND")
    
    return available_cards, missing_cards

def print_detailed_results(available_cards, missing_cards):
    """Print detailed results"""
    print("\n" + "="*60)
    print("✅ CARDS YOU HAVE AVAILABLE:")
    print("="*60)
    
    sufficient_count = 0
    insufficient_count = 0
    
    for card_name, info in available_cards.items():
        status = "✅ SUFFICIENT" if info['sufficient'] else "⚠️  INSUFFICIENT"
        print(f"\n{card_name}")
        print(f"  Need: {info['needed']}, Have: {info['available']} - {status}")
        
        if info['sufficient']:
            sufficient_count += 1
        else:
            insufficient_count += 1
            
        print(f"  Available in {len(info['instances'])} different versions:")
        for instance in info['instances']:
            print(f"    - Quantity: {instance['quantity']}")
    
    print("\n" + "="*60)
    print("❌ CARDS COMPLETELY MISSING:")
    print("="*60)
    
    for card_name, info in missing_cards.items():
        print(f"{card_name} - Need: {info['needed']}, Have: 0")
    
    print(f"\n" + "="*60)
    print("📊 FINAL SUMMARY:")
    print("="*60)
    total_cards = len(available_cards) + len(missing_cards)
    total_needed = sum(info['needed'] for info in available_cards.values()) + sum(info['needed'] for info in missing_cards.values())
    total_available = sum(min(info['available'], info['needed']) for info in available_cards.values())
    
    print(f"Total unique cards required: {total_cards}")
    print(f"Total individual cards needed: {total_needed}")
    print(f"Cards available (sufficient quantity): {sufficient_count}")
    print(f"Cards available (insufficient quantity): {insufficient_count}")
    print(f"Cards completely missing: {len(missing_cards)}")
    print(f"Individual cards you can provide: {total_available}/{total_needed}")
    print(f"Unique card fulfillment rate: {(sufficient_count/total_cards)*100:.1f}%")
    print(f"Individual card fulfillment rate: {(total_available/total_needed)*100:.1f}%")

if __name__ == "__main__":
    available_cards, missing_cards = analyze_all_requirements()
    print_detailed_results(available_cards, missing_cards)
