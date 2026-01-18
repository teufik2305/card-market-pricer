#!/usr/bin/env python3
import pandas as pd
import json
import re

def load_cardlist():
    """Load and parse the cardlist JSON file"""
    try:
        # Try direct JSON loading first
        with open('data/YuGiOh/cardlist.json', 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Direct JSON load failed: {e}")
        try:
            # Fallback: read as string and clean
            with open('data/YuGiOh/cardlist.json', 'r', encoding='utf-8') as f:
                content = f.read()
                
            # Clean up the content
            content = content.strip()
            if content.endswith('%'):
                content = content[:-1]
                
            # Find the last valid JSON closing
            last_brace = content.rfind('}')
            if last_brace != -1:
                content = content[:last_brace + 1]
                
            # Parse JSON
            data = json.loads(content)
        except Exception as e2:
            print(f"Fallback JSON parsing also failed: {e2}")
            return None
    except Exception as e:
        print(f"Error reading file: {e}")
        return None
    
    try:
        
        # Flatten the nested JSON structure
        rows = []
        for set_name, cards in data['Singles'].items():
            for card_name, card_info in cards.items():
                rows.append({
                    'set_name': set_name,
                    'card_name': card_name,
                    'quantity': card_info['quantity'],
                    'price_from': card_info['price_from'],
                    'price_trend': card_info['price_trend'],
                    'price_30_day_avg': card_info['price_30_day_avg']
                })
        
        return pd.DataFrame(rows)
    
    except json.JSONDecodeError as e:
        print(f"JSON parsing error: {e}")
        return None
    except Exception as e:
        print(f"Error loading file: {e}")
        return None

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

def analyze_availability(df, requirements):
    """Analyze card availability against requirements"""
    print("=== BUYER REQUIREMENTS ANALYSIS ===\n")
    
    available_cards = {}
    missing_cards = {}
    
    for card_name, needed_qty in requirements.items():
        # Find all versions of this card
        matching_cards = df[df['card_name'].str.contains(card_name, case=False, na=False)]
        
        if len(matching_cards) > 0:
            # Calculate total available quantity across all sets/versions
            total_available = matching_cards['quantity'].sum()
            
            if total_available > 0:
                available_cards[card_name] = {
                    'needed': needed_qty,
                    'available': total_available,
                    'sufficient': total_available >= needed_qty,
                    'versions': matching_cards[matching_cards['quantity'] > 0][['set_name', 'card_name', 'quantity']].to_dict('records')
                }
            else:
                missing_cards[card_name] = {
                    'needed': needed_qty,
                    'available': 0,
                    'versions_found': len(matching_cards)
                }
        else:
            missing_cards[card_name] = {
                'needed': needed_qty,
                'available': 0,
                'versions_found': 0
            }
    
    return available_cards, missing_cards

def print_results(available_cards, missing_cards):
    """Print detailed results"""
    print("✅ CARDS YOU HAVE AVAILABLE:")
    print("=" * 50)
    
    sufficient_count = 0
    insufficient_count = 0
    
    for card_name, info in available_cards.items():
        status = "✅ SUFFICIENT" if info['sufficient'] else "⚠️  INSUFFICIENT"
        print(f"{card_name}")
        print(f"  Need: {info['needed']}, Have: {info['available']} - {status}")
        
        if info['sufficient']:
            sufficient_count += 1
        else:
            insufficient_count += 1
            
        for version in info['versions']:
            print(f"    - {version['set_name']}: {version['card_name']} (qty: {version['quantity']})")
        print()
    
    print("\n❌ CARDS COMPLETELY MISSING:")
    print("=" * 50)
    
    for card_name, info in missing_cards.items():
        print(f"{card_name} - Need: {info['needed']}, Have: 0")
        if info['versions_found'] > 0:
            print(f"  (Found {info['versions_found']} versions but all have 0 quantity)")
        else:
            print(f"  (No versions found in database)")
    
    print(f"\n📊 SUMMARY:")
    print("=" * 50)
    total_cards = len(available_cards) + len(missing_cards)
    print(f"Total required cards: {total_cards}")
    print(f"Cards available (sufficient): {sufficient_count}")
    print(f"Cards available (insufficient): {insufficient_count}")
    print(f"Cards completely missing: {len(missing_cards)}")
    print(f"Fulfillment rate: {(sufficient_count/total_cards)*100:.1f}%")

if __name__ == "__main__":
    # Load data
    print("Loading cardlist data...")
    df = load_cardlist()
    
    if df is None:
        print("Failed to load data")
        exit(1)
    
    print(f"Loaded {len(df)} total card entries")
    print(f"Cards with quantity > 0: {len(df[df['quantity'] > 0])}")
    
    # Create buyer requirements
    requirements = create_buyer_requirements()
    print(f"Buyer requires {len(requirements)} different cards")
    
    # Analyze availability
    available_cards, missing_cards = analyze_availability(df, requirements)
    
    # Print results
    print_results(available_cards, missing_cards)
