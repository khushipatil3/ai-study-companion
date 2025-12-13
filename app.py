def get_project_details(name):
    """Fetches the full details of a specific project, checking for new columns."""
    conn = sqlite3.connect('study_db.sqlite')
    c = conn.cursor()
    # Fetch all data for the project
    c.execute("SELECT * FROM projects WHERE name=?", (name,))
    row = c.fetchone()
    
    # Get the column names from the description
    columns = [desc[0] for desc in c.description]
    
    conn.close()
    
    if row:
        project_data = {
            "name": row[columns.index("name")],
            "level": row[columns.index("level")],
            "notes": row[columns.index("notes")],
            "raw_text": row[columns.index("raw_text")],
            "progress": row[columns.index("progress")],
            # Safely check for the new column 'analogy_cache'
            "analogy_cache": row[columns.index("analogy_cache")] if "analogy_cache" in columns else None
        }
        return project_data
    return None

# The rest of the code is unchanged.
