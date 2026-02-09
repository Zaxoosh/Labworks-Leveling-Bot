import sqlite3
import csv
import os

# Configuration
CSV_FILE = '1026557557030006855__202602082002.csv'
DB_FILE = 'levels.db'

def import_csv_to_db():
    if not os.path.exists(CSV_FILE):
        print(f"❌ Error: Could not find {CSV_FILE}")
        return

    # Connect to the database
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    print("🔄 Importing data...")
    
    with open(CSV_FILE, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        count = 0
        
        for row in reader:
            try:
                # Map CSV columns to DB columns
                user_id = int(row['userID'])
                guild_id = int(row['guildID'])
                xp = int(row['globalPoints'])
                weekly_xp = int(row['weeklyPoints'])
                level = int(row['uLevel'])
                
                # Insert or Replace into the users table
                # We default monthly_xp to weekly_xp since we don't have separate monthly data
                cursor.execute("""
                    INSERT OR REPLACE INTO users 
                    (user_id, guild_id, xp, weekly_xp, monthly_xp, level, message_count, rebirth)
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0)
                """, (user_id, guild_id, xp, weekly_xp, weekly_xp, level))
                
                count += 1
            except ValueError:
                continue # Skip bad rows

    conn.commit()
    conn.close()
    print(f"✅ Successfully imported {count} users into {DB_FILE}!")

if __name__ == "__main__":
    import_csv_to_db()