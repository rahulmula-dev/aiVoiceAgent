import os
from pinecone import Pinecone
from dotenv import load_dotenv

load_dotenv()

def audit():
    pc = Pinecone(api_key=os.getenv('PINECONE_API_KEY'))
    index_name = os.getenv('PINECONE_INDEX_NAME', 'gd-college')
    index = pc.Index(index_name)
    
    res = index.fetch(ids=['vec_0'])
    if 'vec_0' in res.vectors:
        metadata = res.vectors['vec_0'].metadata
        print("--- LIVE METADATA SAMPLE (vec_0) ---")
        for key, value in metadata.items():
            print(f"{key}: {value} (Type: {type(value).__name__})")
    else:
        print("vec_0 not found")

if __name__ == "__main__":
    audit()
