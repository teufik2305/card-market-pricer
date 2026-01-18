# Card Market Pricer

A comprehensive tool for managing and pricing trading card game collections (YuGiOh and Digimon). This project automates the process of tracking card collections, fetching current market prices, calculating collection worth, and analyzing buyer requirements.

## Features

### Card Data Scraping
- Automated web scraping of card market prices using Selenium
- Support for multiple trading card games (YuGiOh, Digimon)
- Handles various currency formats and price sources
- Robust error handling and retry mechanisms

### Collection Management
- Track card quantities across different expansions
- Calculate collection worth using multiple pricing methods:
  - Current market price (`price_from`)
  - Trend price (`price_trend`)
  - 30-day average price (`price_30_day_avg`)
- Export collection data to Excel and Google Sheets

### Buyer Requirements Analysis
- Analyze your collection against specific card requirements
- Generate detailed reports on card availability
- Identify missing cards and fulfillment rates
- Support for complex deck requirements (monsters, spells, traps)

### Multi-Game Support
- **YuGiOh**: Extensive support for all expansions and card types
- **Digimon**: Full Digimon card game integration
- Modular architecture for easy addition of new games

## Project Structure

```
card-market-pricer/
├── analyze_buyer_requirements.py    # Detailed buyer requirements analysis
├── analyze_buyer_simple.py          # Simplified requirements checker
├── card_collection_pricer.ipynb     # Main pricing and scraping notebook
├── cardlist_scraper.ipynb           # Card data scraping tool
├── card_quantity_updater.ipynb      # Collection quantity management
├── credentials/
│   └── card-market-pricer-44468fb06eaa.json  # Google API credentials
├── data/
│   ├── Digimon/
│   │   ├── cardlist.json                    # Digimon card database
│   │   ├── collection_worth.xlsx           # Digimon collection export
│   │   └── updated-cards-prices.json       # Digimon price data
│   └── YuGiOh/
│       ├── cardlist.json                    # YuGiOh card database
│       ├── collection_worth.xlsx           # YuGiOh collection export
│       ├── cards-to-keep.json              # Cards excluded from analysis
│       └── updated-cards-prices.json       # YuGiOh price data
```

## Prerequisites

- Python 3.8+
- Chrome browser (for Selenium scraping)
- Google Cloud Platform account (for Google Sheets integration)

## Installation

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd card-market-pricer
   ```

2. **Install dependencies:**
   ```bash
   pip install selenium pandas webdriver-manager google-api-python-client google-auth-httplib2 google-auth-oauthlib
   ```

3. **Set up Google API credentials:**
   - Create a Google Cloud Project
   - Enable Google Sheets API
   - Download service account credentials JSON file
   - Place it in the `credentials/` directory

4. **Configure Chrome WebDriver:**
   - The project uses webdriver-manager for automatic ChromeDriver management
   - No manual driver installation required

## Usage

### 1. Card Data Scraping

Run the main pricing notebook:
```bash
jupyter notebook card_collection_pricer.ipynb
```

Key functions:
- `get_price()`: Scrape current market prices for cards
- `setup_driver()`: Initialize Chrome WebDriver with proper configuration
- `get_card_price()`: Extract price data from individual card pages

### 2. Collection Management

Update your card quantities:
```bash
jupyter notebook card_quantity_updater.ipynb
```

Calculate collection worth:
```python
from card_collection_pricer import get_worth

# Calculate total collection value
df = get_worth(df, export=True)  # Exports to Excel
```

### 3. Buyer Requirements Analysis

Analyze card availability for specific requirements:
```bash
python analyze_buyer_requirements.py
```

Or use the simplified version:
```bash
python analyze_buyer_simple.py
```

### 4. Google Sheets Integration

Export collection data to Google Sheets:
```python
from card_collection_pricer import get_worth_google_sheets

spreadsheet_id = "your-google-sheet-id"
get_worth_google_sheets(df, spreadsheet_id)
```

## Configuration

### Game Selection
Set the target game in notebooks:
```python
tcg = 'YuGiOh'  # or 'Digimon'
```

### Expansion Selection
Configure which expansions to analyze:
```python
my_ygo_expansions = [
    'Age-of-Overlord',
    'Amazing-Defenders',
    # ... add more expansions
]
```

### Cards to Exclude
Create a `cards-to-keep.json` file to exclude certain cards from analysis:
```json
{
  "Singles": {
    "expansion-name": {
      "card-name": {"quantity": 1}
    }
  }
}
```

## Data Format

### Card Database Structure
```json
{
  "Singles": {
    "expansion-name": {
      "card-name": {
        "quantity": 1,
        "price_from": 2.50,
        "price_trend": 3.00,
        "price_30_day_avg": 2.75
      }
    }
  }
}
```

### Collection Worth Export
The Excel export includes:
- Individual card values
- Expansion subtotals
- Grand totals
- Multiple pricing columns

## API Integration

### Google Sheets
- Automatic authentication using service account
- Hierarchical data structure (expansions → cards)
- Real-time updates and sharing capabilities

### Card Market API
- Web scraping of cardmarket.com
- Handles rate limiting and session management
- Currency conversion and formatting

## Error Handling

The project includes robust error handling for:
- Network timeouts and connection issues
- Invalid card data or missing prices
- Google API authentication failures
- File I/O operations
- Session management and browser crashes

## Performance Considerations

- Random delays between requests to avoid rate limiting
- Progress saving during long scraping operations
- Modular processing for large collections
- Memory-efficient data processing with pandas

## Contributing

1. Fork the repository
2. Create a feature branch
3. Add tests for new functionality
4. Ensure all notebooks run without errors
5. Submit a pull request

## Troubleshooting

### Common Issues

1. **Chrome WebDriver issues:**
   - Ensure Chrome browser is installed and up to date
   - Check Chrome version compatibility with webdriver-manager

2. **Google API errors:**
   - Verify service account credentials are valid
   - Check Google Sheets API is enabled in your project
   - Ensure spreadsheet sharing permissions are correct

3. **Data parsing errors:**
   - Validate JSON file structure
   - Check for encoding issues in card names
   - Verify expansion names match the database

### Debug Mode
Enable verbose logging by modifying the logging configuration in notebooks.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Disclaimer

This tool is for personal collection management and educational purposes. Please respect the terms of service of card market websites and APIs. The authors are not responsible for any misuse of this software.