import urllib.parse
import urllib.request
import json

def download_track_from_youtube(artist: str, title: str, output_path: str = "downloads"):
    os.makedirs(output_path, exist_ok=True)
    
    clean_query = f"{artist} - {title}".replace('"', '').replace("'", "")
    
    # Используем альтернативный метод формирования поисковой ссылки, 
    # чтобы обойти блокировку ytsearch на серверных IP
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'outtmpl': os.path.join(output_path, '%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        }
    }
    
    # Пробуем разные вариации запросов с разными клиентами YouTube внутри yt-dlp
    search_queries = [
        f"ytsearch1:{clean_query} topic",
        f"ytsearch1:{clean_query}",
        f"ytsearch1:{artist} {title} audio"
    ]
    
    for q in search_queries:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(q, download=True)
                filename = ydl.prepare_filename(info)
                mp3_filename = os.path.splitext(filename)[0] + ".mp3"
                if os.path.exists(mp3_filename):
                    return mp3_filename
        except Exception as e:
            continue
            
    return None
