from flask import Flask, render_template, request, jsonify, send_file
import os
import random
import numpy as np
import threading
import time

# Global job status tracker for async processing
job_status = {}
job_results = {}

try:
    from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips, CompositeAudioClip, concatenate_audioclips, TextClip, CompositeVideoClip
except Exception:
    from moviepy import VideoFileClip, AudioFileClip, concatenate_videoclips, CompositeAudioClip, concatenate_audioclips, TextClip, CompositeVideoClip
from werkzeug.utils import secure_filename
import uuid
import shutil

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['OUTPUT_FOLDER'] = 'outputs'
app.config['MUSIC_FOLDER'] = 'music'
app.config['FONT_FOLDER'] = 'fonts'
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB max

for folder in [app.config['UPLOAD_FOLDER'], app.config['OUTPUT_FOLDER'], app.config['MUSIC_FOLDER'], app.config['FONT_FOLDER']]:
    if not os.path.exists(folder):
        os.makedirs(folder)

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'MOV', 'MP4'}
ALLOWED_MUSIC_EXTENSIONS = {'mp3', 'wav', 'aac', 'm4a', 'ogg', 'flac'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def allowed_music_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_MUSIC_EXTENSIONS

def get_available_fonts():
    """Scan fonts folder and return list of available font files"""
    font_folder = app.config['FONT_FOLDER']
    fonts = []
    font_extensions = {'.ttf', '.otf', '.woff', '.woff2'}
    
    if os.path.exists(font_folder):
        for filename in os.listdir(font_folder):
            ext = os.path.splitext(filename)[1].lower()
            if ext in font_extensions:
                # Clean font name for display
                name = os.path.splitext(filename)[0].replace('-', ' ').replace('_', ' ')
                fonts.append({'name': name, 'file': filename, 'path': os.path.join(font_folder, filename)})
    
    return sorted(fonts, key=lambda x: x['name'])

def _subclip_compat(clip, start, end):
    """Compatibility wrapper for subclip that works with both old and new moviepy"""
    try:
        # Try new moviepy API first (v2.0+)
        return clip.subclipped(start, end)
    except Exception:
        try:
            # Fall back to old moviepy API (v1.x)
            return clip.subclip(start, end)
        except Exception as e:
            print(f"  Subclip failed: {e}")
            return None

def detect_interesting_moments(video_clip, num_clips=2):
    """Auto-detect best moments using motion detection"""
    if video_clip is None:
        return [(0, 2)]

    # Get duration for fallback
    try:
        duration = video_clip.duration
        if duration is None or duration <= 0:
            return [(0, 2)]
    except Exception:
        return [(0, 2)]

    if duration < 2:
        return [(0, min(duration, 2))]

    # For HEVC or problematic codecs, skip frame analysis and use time-based segments
    # Try once to see if we can get frames
    can_read_frames = False
    try:
        if hasattr(video_clip, 'get_frame') and video_clip.reader is not None:
            test_frame = video_clip.get_frame(0)
            if test_frame is not None:
                can_read_frames = True
    except Exception as e:
        print(f"  Frame reading test failed: {e}")
        can_read_frames = False
    
    if not can_read_frames:
        # Fallback: use a single random segment per video (no repetition)
        segment = min(4, duration * 0.4)  # Take up to 40% of video or 4 seconds
        print(f"  Using time-based segment (no frame analysis)")
        # Use random offset between 10% and 50% to get varied content
        offset = random.uniform(0.1, 0.5)
        start = duration * offset
        # Ensure we don't exceed video duration
        if start + segment > duration:
            start = max(0, duration - segment - 0.1)
        return [(start, segment)]
    
    sample_count = min(15, int(duration))
    sample_interval = duration / sample_count
    
    motion_scores = []
    
    for i in range(sample_count):
        time = i * sample_interval
        if time + 1 > duration:
            break
        
        try:
            frame1 = video_clip.get_frame(time)
            frame2 = video_clip.get_frame(min(time + 0.5, duration - 0.1))

            if frame1 is None or frame2 is None:
                continue
            
            diff = np.mean(np.abs(frame1.astype(float) - frame2.astype(float)))
            motion_scores.append((time, diff))
        except Exception as e:
            print(f"  Frame analysis failed at {time}s: {e}")
            continue
    
    if not motion_scores:
        segment = min(4, duration * 0.4)  # Single segment, no repetition
        print(f"  No motion analysis, using single segment")
        offset = random.uniform(0.15, 0.55)
        start = duration * offset
        if start + segment > duration:
            start = max(0, duration - segment - 0.1)
        return [(start, segment)]
    
    motion_scores.sort(key=lambda x: x[1], reverse=True)
    
    selected_moments = []
    for i in range(min(num_clips, len(motion_scores))):
        start_time = motion_scores[i][0]
        clip_duration = random.uniform(2.5, 4)
        
        if start_time + clip_duration > duration:
            clip_duration = duration - start_time - 0.1
            if clip_duration < 1:
                start_time = max(0, duration - 4)
                clip_duration = min(4, duration - start_time - 0.1)
        
        selected_moments.append((start_time, clip_duration))
    
    return selected_moments

def create_reel(video_paths, video_settings=None, captions=None, music_path=None, 
                mute_videos=False, text_style=None, music_fade=2, output_filename='reel.mp4'):
    """Create reel from uploaded videos with optional trims, captions, and music"""
    import traceback
    clips_to_compile = []
    source_clips = []
    audio_clips = []
    
    # Sort videos by order if video_settings provided
    if video_settings:
        # Create mapping of filename to settings
        settings_map = {}
        for vs in video_settings:
            # Find matching video file by order
            if vs['order'] < len(video_paths):
                settings_map[video_paths[vs['order']]] = vs
    else:
        settings_map = {}
    
    for i, video_path in enumerate(video_paths):
        video_clip = None
        try:
            print(f"Processing video {i+1}: {video_path}")
            
            if not os.path.exists(video_path):
                print(f"  ERROR: File does not exist")
                continue
                
            video_clip = VideoFileClip(video_path)
            
            if video_clip is None:
                print(f"  ERROR: Could not load video (returned None)")
                continue

            duration = video_clip.duration
            if duration is None or duration <= 0:
                print(f"  ERROR: Video has no valid duration")
                video_clip.close()
                continue
            
            print(f"  Duration: {duration}")
            
            # Get trim settings for this video
            trim_start = 0
            trim_end = duration
            
            if video_path in settings_map:
                vs = settings_map[video_path]
                trim_start = vs.get('trim_start', 0)
                trim_end = vs.get('trim_end', duration)
                # Validate trim times
                trim_start = max(0, min(trim_start, duration))
                trim_end = max(trim_start + 0.5, min(trim_end, duration))
                print(f"  Trimming: {trim_start:.2f}s to {trim_end:.2f}s")
            else:
                print(f"  Using full duration (no trim settings)")
            
            # Create trimmed clip
            clip_duration = trim_end - trim_start
            if clip_duration < 0.5:
                print(f"  ERROR: Trimmed clip too short")
                video_clip.close()
                continue
            
            clip = _subclip_compat(video_clip, trim_start, trim_end)
            
            if clip is None:
                print(f"  ERROR: Subclip returned None")
                video_clip.close()
                continue
            
            print(f"    Resizing...")
            clip = clip.resized(height=720)
            
            # Mute video if requested
            if mute_videos and clip.audio is not None:
                print(f"    Muting video audio...")
                clip = clip.without_audio()
            
            print(f"    Cropping...")
            if clip.w / clip.h > 9/16:
                target_width = int(clip.h * 9/16)
                x_center = clip.w / 2
                x1 = int(x_center - target_width / 2)
                clip = clip.cropped(x1=x1, width=target_width)
            
            print(f"    Adding to compile list")
            clips_to_compile.append(clip)
            source_clips.append(video_clip)
                    
        except Exception as e:
            print(f"Error processing {video_path}: {e}")
            traceback.print_exc()
            if video_clip and video_clip not in source_clips:
                try:
                    video_clip.close()
                except:
                    pass
            continue
    
    if not clips_to_compile:
        for clip in source_clips:
            try:
                clip.close()
            except:
                pass
        raise Exception("No clips could be extracted from any video")
    
    print(f"Concatenating {len(clips_to_compile)} clips...")
    try:
        final_clip = concatenate_videoclips(clips_to_compile, method="compose")
        print(f"Concatenation successful - final duration: {final_clip.duration:.2f}s")
    except Exception as e:
        print(f"Concatenation failed: {e}")
        traceback.print_exc()
        for clip in source_clips:
            try:
                clip.close()
            except:
                pass
        raise
    
    # Add captions with timing and global styling
    if captions and len(captions) > 0:
        try:
            from moviepy import TextClip, CompositeVideoClip
            
            # Get global text style defaults
            style = text_style or {}
            global_font = style.get('font', 'Arial')
            global_font_size = style.get('fontSize', 70)
            global_position = style.get('position', 'bottom')
            global_color = style.get('color', 'white')
            
            caption_clips = []
            for cap in captions:
                text = cap.get('text', '').strip()
                if not text:
                    continue
                    
                start_time = cap.get('startTime', 0)
                end_time = cap.get('endTime', 3)
                # Use caption-specific position/color or fall back to global
                position = cap.get('position') or global_position
                color = cap.get('color') or global_color
                
                # Validate times against final clip duration
                duration = final_clip.duration
                start_time = max(0, min(start_time, duration))
                end_time = max(start_time + 0.1, min(end_time, duration))
                
                # Map position
                position_map = {
                    'top': ('center', 100),
                    'center': ('center', 'center'),
                    'bottom': ('center', final_clip.h - 150)
                }
                pos = position_map.get(position, ('center', final_clip.h - 150))
                
                # Map color to stroke
                stroke_color = 'black' if color in ['white', 'yellow'] else 'white'
                
                # Get font path - check custom fonts first, then fall back to Windows fonts
                font_to_use = None
                
                # First check if the requested font is in our custom fonts folder
                custom_fonts = get_available_fonts()
                font_name = global_font.replace(' ', '').replace('-', '').replace('_', '').lower()
                
                for custom_font in custom_fonts:
                    custom_name = custom_font['name'].replace(' ', '').replace('-', '').replace('_', '').lower()
                    if font_name == custom_name or font_name in custom_name:
                        font_to_use = custom_font['path']
                        break
                
                # If no custom font found, try Windows system fonts
                if not font_to_use:
                    import platform
                    if platform.system() == 'Windows':
                        windows_fonts = {
                            'Arial': 'C:/Windows/Fonts/arial.ttf',
                            'Courier': 'C:/Windows/Fonts/cour.ttf',
                            'Times': 'C:/Windows/Fonts/times.ttf',
                            'Verdana': 'C:/Windows/Fonts/verdana.ttf',
                            'Georgia': 'C:/Windows/Fonts/georgia.ttf'
                        }
                        if global_font in windows_fonts and os.path.exists(windows_fonts[global_font]):
                            font_to_use = windows_fonts[global_font]
                        elif os.path.exists(windows_fonts['Arial']):
                            font_to_use = windows_fonts['Arial']
                
                # Build TextClip kwargs
                textclip_kwargs = {
                    'text': text,
                    'font_size': global_font_size,
                    'color': color,
                    'stroke_color': stroke_color,
                    'stroke_width': 3,
                    'duration': end_time - start_time,
                    'size': (final_clip.w - 100, None)
                }
                if font_to_use and os.path.exists(font_to_use):
                    textclip_kwargs['font'] = font_to_use
                
                txt_clip = TextClip(**textclip_kwargs).with_position(pos)
                
                # Set start time
                txt_clip = txt_clip.with_start(start_time)
                caption_clips.append(txt_clip)
                print(f"Caption: '{text[:30]}...' at {start_time:.1f}s-{end_time:.1f}s, {position}")
            
            if caption_clips:
                final_clip = CompositeVideoClip([final_clip] + caption_clips)
                print(f"Added {len(caption_clips)} captions")
        except Exception as e:
            print(f"Could not add captions: {e}")
            traceback.print_exc()
    
    # Add music if provided with fade in/out
    if music_path and os.path.exists(music_path):
        try:
            print(f"Adding music from: {music_path}")
            music_clip = AudioFileClip(music_path)
            audio_clips.append(music_clip)
            
            if music_clip.duration < final_clip.duration:
                print(f"  Music shorter than video, looping...")
                loops_needed = int(final_clip.duration / music_clip.duration) + 1
                music_clip = concatenate_audioclips([music_clip] * loops_needed).subclipped(0, final_clip.duration)
                audio_clips.append(music_clip)
            else:
                music_clip = music_clip.subclipped(0, final_clip.duration)
            
            # Apply fade in/out using effects
            if music_fade > 0:
                print(f"  Applying {music_fade}s fade in/out...")
                try:
                    from moviepy.audio.fx.AudioFadeIn import AudioFadeIn
                    from moviepy.audio.fx.AudioFadeOut import AudioFadeOut
                    music_clip = music_clip.with_effects([AudioFadeIn(music_fade), AudioFadeOut(music_fade)])
                except Exception as fade_error:
                    print(f"  Fade effect failed: {fade_error}, continuing without fade...")
            
            # Apply volume reduction using multiply_volume
            try:
                from moviepy.audio.fx.MultiplyVolume import multiply_volume
                music_clip = music_clip.with_effects([multiply_volume(0.3)])
            except Exception as vol_error:
                print(f"  Volume adjustment failed: {vol_error}, continuing at full volume...")
            final_clip = final_clip.with_audio(music_clip)
            print(f"Music added successfully")
        except Exception as e:
            print(f"Could not add music: {e}")
            traceback.print_exc()
    
    output_path = os.path.join(app.config['OUTPUT_FOLDER'], output_filename)
    
    print(f"Writing video to {output_path}...")
    try:
        final_clip.write_videofile(
            output_path,
            codec='libx264',
            audio=True,
            audio_codec='aac',
            audio_bitrate='128k',
            fps=30,
            preset='ultrafast',
            bitrate='2500k',
            threads=8,
            logger=None
        )
        print(f"Video written successfully")
    except Exception as e:
        print(f"Video write failed: {e}")
        traceback.print_exc()
        for clip in source_clips:
            try:
                clip.close()
            except:
                pass
        for clip in audio_clips:
            try:
                clip.close()
            except:
                pass
        raise
    
    final_clip.close()
    for clip in clips_to_compile:
        try:
            clip.close()
        except:
            pass
    for clip in source_clips:
        try:
            clip.close()
        except:
            pass
    for clip in audio_clips:
        try:
            clip.close()
        except:
            pass
    
    return output_path

def create_reel_async(session_id, video_paths, video_settings=None, captions=None, music_path=None, 
                mute_videos=False, text_style=None, music_fade=2, output_filename='reel.mp4'):
    """Create reel asynchronously and update job status"""
    try:
        output_path = create_reel(
            video_paths, 
            video_settings=video_settings,
            captions=captions,
            music_path=music_path,
            mute_videos=mute_videos,
            text_style=text_style,
            music_fade=music_fade,
            output_filename=output_filename
        )
        job_status[session_id] = 'completed'
        job_results[session_id] = {'success': True, 'download_url': f'/download/{session_id}'}
    except Exception as e:
        import traceback
        print("ASYNC RENDER FAILED:")
        traceback.print_exc()
        job_status[session_id] = 'failed'
        job_results[session_id] = {'error': str(e)}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_files():
    if 'videos' not in request.files:
        return jsonify({'error': 'No videos uploaded'}), 400
    
    files = request.files.getlist('videos')
    
    if not files:
        return jsonify({'error': 'No videos selected'}), 400
    
    session_id = str(uuid.uuid4())
    session_folder = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    os.makedirs(session_folder)
    
    uploaded_files = []
    for file in files:
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(session_folder, filename)
            file.save(filepath)
            uploaded_files.append(filepath)
    
    if not uploaded_files:
        return jsonify({'error': 'No valid video files'}), 400
    
    return jsonify({
        'session_id': session_id,
        'files_uploaded': len(uploaded_files),
        'files': [os.path.basename(f) for f in uploaded_files]
    })

@app.route('/upload_music', methods=['POST'])
def upload_music():
    """Upload local music file"""
    if 'music' not in request.files:
        return jsonify({'error': 'No music file uploaded'}), 400
    
    file = request.files['music']
    
    if not file or file.filename == '':
        return jsonify({'error': 'No music file selected'}), 400
    
    if not allowed_music_file(file.filename):
        return jsonify({'error': 'Invalid music file type. Allowed: mp3, wav, aac, m4a, ogg, flac'}), 400
    
    session_id = str(uuid.uuid4())
    filename = secure_filename(file.filename)
    music_path = os.path.join(app.config['MUSIC_FOLDER'], f"{session_id}_{filename}")
    file.save(music_path)
    
    return jsonify({
        'success': True,
        'music_path': music_path,
        'filename': filename
    })

@app.route('/create', methods=['POST'])
def create_reel_endpoint():
    import traceback
    data = request.json
    session_id = data.get('session_id')
    video_settings = data.get('video_settings', [])
    captions = data.get('captions', [])
    music_source = data.get('music_source', 'none')
    music_path = data.get('music_path', None)
    text_style = data.get('text_style', None)
    mute_videos = data.get('mute_videos', False)
    music_fade = data.get('music_fade', 2)
    
    if not session_id:
        return jsonify({'error': 'No session ID'}), 400
    
    session_folder = os.path.join(app.config['UPLOAD_FOLDER'], session_id)
    
    if not os.path.exists(session_folder):
        return jsonify({'error': 'Session not found'}), 404
    
    # Get video files and sort by order if video_settings provided
    video_files = [os.path.join(session_folder, f) for f in os.listdir(session_folder) 
                   if allowed_file(f)]

    if not video_files:
        return jsonify({'error': 'No videos found'}), 400

    # Sort files by video_settings order if provided
    if video_settings and len(video_settings) > 0:
        # Build ordered list based on video_settings order
        # video_settings contains objects with {order, trim_start, trim_end, filename}
        ordered_files = []
        settings_map = {}
        
        # Sort by order field and build ordered list
        sorted_settings = sorted(video_settings, key=lambda x: x.get('order', 0))
        
        print(f"Ordering {len(sorted_settings)} videos by user selection:")
        for vs in sorted_settings:
            filename = vs.get('filename', '')
            print(f"  Order {vs.get('order')}: {filename}")
            if filename in [os.path.basename(f) for f in video_files]:
                filepath = [f for f in video_files if os.path.basename(f) == filename][0]
                ordered_files.append(filepath)
                settings_map[filepath] = vs
        
        if ordered_files:
            video_files = ordered_files
            print(f"Successfully ordered {len(video_files)} videos")
    else:
        settings_map = {}

    final_music_path = None
    if music_source == 'local' and music_path:
        if os.path.exists(music_path):
            final_music_path = music_path
        else:
            return jsonify({'error': 'Music file not found'}), 400

    job_status[session_id] = 'processing'
    threading.Thread(target=create_reel_async, args=(session_id, video_files, video_settings, captions, final_music_path, mute_videos, text_style, music_fade, f'{session_id}.mp4')).start()
    
    return jsonify({
        'success': True,
        'status': 'processing',
        'session_id': session_id
    })

@app.route('/status/<session_id>')
def check_status(session_id):
    """Check video processing status"""
    status = job_status.get(session_id, 'unknown')
    
    if status == 'completed':
        result = job_results.get(session_id, {})
        job_status.pop(session_id, None)
        job_results.pop(session_id, None)
        return jsonify({'status': 'completed', **result})
    elif status == 'failed':
        result = job_results.get(session_id, {'error': 'Unknown error'})
        job_status.pop(session_id, None)
        job_results.pop(session_id, None)
        return jsonify({'status': 'failed', **result})
    elif status == 'processing':
        return jsonify({'status': 'processing'})
    else:
        output_path = os.path.join(app.config['OUTPUT_FOLDER'], f'{session_id}.mp4')
        if os.path.exists(output_path):
            return jsonify({'status': 'completed', 'download_url': f'/download/{session_id}'})
        return jsonify({'status': 'not_found'})

@app.route('/fonts')
def list_fonts():
    """Return list of available fonts from the fonts folder"""
    fonts = get_available_fonts()
    return jsonify({'fonts': fonts})

@app.route('/download/<session_id>')
def download(session_id):
    filename = f'{session_id}.mp4'
    filepath = os.path.join(app.config['OUTPUT_FOLDER'], filename)
    
    if not os.path.exists(filepath):
        return "File not found", 404
    
    return send_file(filepath, as_attachment=True, download_name='pet_reel.mp4')

@app.route('/video/<session_id>/<filename>')
def serve_video(session_id, filename):
    """Serve uploaded video file for preview"""
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], session_id, filename)
    
    if not os.path.exists(filepath):
        return "File not found", 404
    
    return send_file(filepath)

@app.route('/clear_storage', methods=['POST'])
def clear_storage():
    """Clear all uploads, outputs, and music files"""
    try:
        uploads_deleted = 0
        outputs_deleted = 0
        music_deleted = 0
        
        # Clear uploads folder
        upload_folder = app.config['UPLOAD_FOLDER']
        if os.path.exists(upload_folder):
            for filename in os.listdir(upload_folder):
                filepath = os.path.join(upload_folder, filename)
                try:
                    if os.path.isfile(filepath):
                        os.remove(filepath)
                        uploads_deleted += 1
                except Exception as e:
                    print(f"Error deleting {filepath}: {e}")
        
        # Clear outputs folder
        output_folder = app.config['OUTPUT_FOLDER']
        if os.path.exists(output_folder):
            for filename in os.listdir(output_folder):
                filepath = os.path.join(output_folder, filename)
                try:
                    if os.path.isfile(filepath):
                        os.remove(filepath)
                        outputs_deleted += 1
                except Exception as e:
                    print(f"Error deleting {filepath}: {e}")
        
        # Clear music folder
        music_folder = app.config['MUSIC_FOLDER']
        if os.path.exists(music_folder):
            for filename in os.listdir(music_folder):
                filepath = os.path.join(music_folder, filename)
                try:
                    if os.path.isfile(filepath):
                        os.remove(filepath)
                        music_deleted += 1
                except Exception as e:
                    print(f"Error deleting {filepath}: {e}")
        
        return jsonify({
            'success': True,
            'uploads_deleted': uploads_deleted,
            'outputs_deleted': outputs_deleted,
            'music_deleted': music_deleted
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)