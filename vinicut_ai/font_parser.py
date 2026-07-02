import struct
import os

def parse_font_family(font_path):
    """
    Parses a TTF or OTF file to extract the correct font family name (Name ID 1).
    Falls back to Name ID 4 (Full name) or filename if parsing fails.
    """
    if not os.path.exists(font_path):
        return os.path.splitext(os.path.basename(font_path))[0]

    try:
        with open(font_path, "rb") as f:
            # 1. Read Offset Table (12 bytes)
            offset_data = f.read(12)
            if len(offset_data) < 12:
                return os.path.splitext(os.path.basename(font_path))[0]
                
            scaler_type, num_tables, search_range, entry_selector, range_shift = struct.unpack(">IHHHH", offset_data)
            
            # 2. Iterate Table Directory Entries (16 bytes each)
            name_table_offset = None
            name_table_length = None
            for _ in range(num_tables):
                entry_data = f.read(16)
                if len(entry_data) < 16:
                    break
                tag, checksum, offset, length = struct.unpack(">4sIII", entry_data)
                if tag == b"name":
                    name_table_offset = offset
                    name_table_length = length
                    break
                    
            if name_table_offset is None:
                return os.path.splitext(os.path.basename(font_path))[0]
                
            # 3. Read Name Table
            f.seek(name_table_offset)
            name_table_data = f.read(name_table_length)
            if len(name_table_data) < 6:
                return os.path.splitext(os.path.basename(font_path))[0]
                
            format_sel, count, string_offset = struct.unpack(">HHH", name_table_data[:6])
            
            # Keep track of matches
            family_name = None
            full_name = None
            
            # 4. Scan Name Records (12 bytes each)
            for idx in range(count):
                record_offset = 6 + (idx * 12)
                if record_offset + 12 > len(name_table_data):
                    break
                    
                platform_id, encoding_id, language_id, name_id, length, offset = struct.unpack(
                    ">HHHHHH", name_table_data[record_offset:record_offset+12]
                )
                
                # We want Name ID 1 (Font Family) or Name ID 4 (Full Font Name)
                if name_id in (1, 4):
                    string_start = string_offset + offset
                    string_end = string_start + length
                    if string_end <= len(name_table_data):
                        raw_bytes = name_table_data[string_start:string_end]
                        
                        # Decode string based on Platform/Encoding
                        decoded = None
                        try:
                            if platform_id == 3:  # Windows (always UTF-16BE)
                                decoded = raw_bytes.decode("utf-16-be")
                            elif platform_id == 0:  # Unicode (always UTF-16BE)
                                decoded = raw_bytes.decode("utf-16-be")
                            elif platform_id == 1:  # Mac Roman (ASCII/Latin1)
                                decoded = raw_bytes.decode("latin-1")
                            else:
                                # Fallback trial
                                decoded = raw_bytes.decode("utf-16-be")
                        except Exception:
                            try:
                                decoded = raw_bytes.decode("utf-8", errors="ignore")
                            except Exception:
                                pass
                                
                        if decoded:
                            # Strip null characters (sometimes present in font strings)
                            decoded = decoded.replace("\x00", "").strip()
                            if decoded:
                                if name_id == 1:
                                    family_name = decoded
                                elif name_id == 4:
                                    full_name = decoded
                                    
            if family_name:
                return family_name
            if full_name:
                return full_name
                
    except Exception as e:
        print(f"Error parsing font metadata: {e}")
        
    # Return file basename (without extension) if parser fails or doesn't find name record
    return os.path.splitext(os.path.basename(font_path))[0]
