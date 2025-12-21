#!/usr/bin/env python3
"""
File Chunker for Large C Files

Splits large C files into reviewable chunks based on function boundaries.
This allows the AI reviewer to handle files of any size by reviewing one
function at a time while maintaining context.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class CodeChunk:
    """Represents a chunk of code to review."""
    file_path: Path
    start_line: int
    end_line: int
    content: str
    chunk_type: str  # 'header', 'function', 'global', 'footer'
    name: Optional[str] = None  # Function name for functions
    
    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


class CFileChunker:
    """
    Chunks C source files into reviewable pieces.
    
    Strategy:
    1. Files < 800 lines: Return as single chunk
    2. Files 800-2000 lines: Split by functions, group small ones
    3. Files > 2000 lines: One chunk per function (or per 500 lines for large functions)
    """
    
    # Regex to find function definitions (simplified, handles most cases)
    FUNCTION_RE = re.compile(
        r'^(?:static\s+)?(?:inline\s+)?'  # Optional static/inline
        r'(?:const\s+)?'  # Optional const
        r'(?:unsigned\s+)?(?:signed\s+)?'  # Optional signed/unsigned
        r'(?:struct\s+\w+\s+\*?|'  # struct type
        r'enum\s+\w+\s+|'  # enum type
        r'void|int|long|short|char|float|double|size_t|ssize_t|'  # Basic types
        r'uint\d+_t|int\d+_t|'  # stdint types
        r'\w+_t)\s*\*?\s+'  # Custom typedef
        r'(\w+)\s*\(',  # Function name and opening paren
        re.MULTILINE
    )
    
    def __init__(self, max_chunk_lines: int = 500, small_file_threshold: int = 800):
        self.max_chunk_lines = max_chunk_lines
        self.small_file_threshold = small_file_threshold
    
    def should_chunk(self, file_path: Path) -> bool:
        """Determine if a file needs chunking."""
        try:
            lines = file_path.read_text(encoding='utf-8', errors='replace').splitlines()
            return len(lines) > self.small_file_threshold
        except Exception:
            return False
    
    def chunk_file(self, file_path: Path) -> List[CodeChunk]:
        """
        Split a C file into chunks.
        
        Returns:
            List of CodeChunk objects
        """
        try:
            content = file_path.read_text(encoding='utf-8', errors='replace')
            lines = content.splitlines(keepends=True)
        except Exception as e:
            # Return error chunk
            return [CodeChunk(
                file_path=file_path,
                start_line=1,
                end_line=1,
                content=f"ERROR: Cannot read file: {e}",
                chunk_type='error'
            )]
        
        # Small files: return as single chunk
        if len(lines) <= self.small_file_threshold:
            return [CodeChunk(
                file_path=file_path,
                start_line=1,
                end_line=len(lines),
                content=content,
                chunk_type='full_file'
            )]
        
        # Large files: chunk by functions
        return self._chunk_by_functions(file_path, lines)
    
    def _chunk_by_functions(self, file_path: Path, lines: List[str]) -> List[CodeChunk]:
        """Chunk a file by function boundaries."""
        chunks = []
        content = ''.join(lines)
        
        # Find all function definitions
        functions = []
        for match in self.FUNCTION_RE.finditer(content):
            func_name = match.group(1)
            func_start_pos = match.start()
            
            # Find line number for this position
            line_num = content[:func_start_pos].count('\n') + 1
            
            # Find the function body end (simplified: find matching brace)
            func_end_line = self._find_function_end(lines, line_num)
            
            functions.append({
                'name': func_name,
                'start_line': line_num,
                'end_line': func_end_line
            })
        
        # If we couldn't find functions, chunk by line count
        if not functions:
            return self._chunk_by_lines(file_path, lines)
        
        # Create header chunk (everything before first function)
        if functions[0]['start_line'] > 1:
            header_content = ''.join(lines[:functions[0]['start_line']-1])
            chunks.append(CodeChunk(
                file_path=file_path,
                start_line=1,
                end_line=functions[0]['start_line'] - 1,
                content=header_content,
                chunk_type='header',
                name='file_header'
            ))
        
        # Create chunks for each function
        for i, func in enumerate(functions):
            func_lines = lines[func['start_line']-1:func['end_line']]
            
            # If function is huge, split it further
            if len(func_lines) > self.max_chunk_lines * 2:
                # Split large functions into sub-chunks
                sub_chunks = self._split_large_function(
                    file_path, func, func_lines
                )
                chunks.extend(sub_chunks)
            else:
                # Include some context between functions if reasonable
                context_end = func['end_line']
                if i + 1 < len(functions):
                    next_func_start = functions[i+1]['start_line']
                    # Include up to 20 lines of context (globals, comments)
                    context_lines = min(20, next_func_start - func['end_line'] - 1)
                    context_end = func['end_line'] + context_lines
                
                chunk_content = ''.join(lines[func['start_line']-1:context_end])
                chunks.append(CodeChunk(
                    file_path=file_path,
                    start_line=func['start_line'],
                    end_line=context_end,
                    content=chunk_content,
                    chunk_type='function',
                    name=func['name']
                ))
        
        # Create footer chunk (everything after last function)
        if functions:
            last_func_end = functions[-1]['end_line']
            if last_func_end < len(lines):
                footer_content = ''.join(lines[last_func_end:])
                if footer_content.strip():  # Only if there's actual content
                    chunks.append(CodeChunk(
                        file_path=file_path,
                        start_line=last_func_end + 1,
                        end_line=len(lines),
                        content=footer_content,
                        chunk_type='footer',
                        name='file_footer'
                    ))
        
        return chunks
    
    def _find_function_end(self, lines: List[str], start_line: int) -> int:
        """
        Find the end of a function by matching braces.
        
        Args:
            lines: File lines
            start_line: Line number where function starts (1-indexed)
            
        Returns:
            Line number where function ends (1-indexed)
        """
        brace_count = 0
        in_function = False
        
        for i in range(start_line - 1, len(lines)):
            line = lines[i]
            
            # Count braces
            for char in line:
                if char == '{':
                    brace_count += 1
                    in_function = True
                elif char == '}':
                    brace_count -= 1
                    if in_function and brace_count == 0:
                        return i + 1  # Return 1-indexed line number
        
        # If we couldn't find the end, return a reasonable default
        return min(start_line + 100, len(lines))
    
    def _split_large_function(
        self, 
        file_path: Path, 
        func: dict, 
        func_lines: List[str]
    ) -> List[CodeChunk]:
        """Split a very large function into sub-chunks."""
        chunks = []
        num_sub_chunks = (len(func_lines) + self.max_chunk_lines - 1) // self.max_chunk_lines
        
        for i in range(num_sub_chunks):
            start_idx = i * self.max_chunk_lines
            end_idx = min((i + 1) * self.max_chunk_lines, len(func_lines))
            
            chunk_content = ''.join(func_lines[start_idx:end_idx])
            chunks.append(CodeChunk(
                file_path=file_path,
                start_line=func['start_line'] + start_idx,
                end_line=func['start_line'] + end_idx - 1,
                content=chunk_content,
                chunk_type='function_part',
                name=f"{func['name']}_part{i+1}"
            ))
        
        return chunks
    
    def _chunk_by_lines(self, file_path: Path, lines: List[str]) -> List[CodeChunk]:
        """Fallback: chunk by fixed line count."""
        chunks = []
        num_chunks = (len(lines) + self.max_chunk_lines - 1) // self.max_chunk_lines
        
        for i in range(num_chunks):
            start_idx = i * self.max_chunk_lines
            end_idx = min((i + 1) * self.max_chunk_lines, len(lines))
            
            chunk_content = ''.join(lines[start_idx:end_idx])
            chunks.append(CodeChunk(
                file_path=file_path,
                start_line=start_idx + 1,
                end_line=end_idx,
                content=chunk_content,
                chunk_type='line_chunk',
                name=f'chunk_{i+1}'
            ))
        
        return chunks


def format_chunk_for_review(chunk: CodeChunk, total_chunks: int, chunk_num: int) -> str:
    """Format a chunk for presentation to the AI reviewer."""
    header = f"=== CHUNK {chunk_num}/{total_chunks}: {chunk.file_path} ==="
    if chunk.name:
        header += f" [{chunk.chunk_type}: {chunk.name}]"
    header += f"\n=== Lines {chunk.start_line}-{chunk.end_line} ({chunk.line_count} lines) ===\n\n"
    
    return header + chunk.content


if __name__ == "__main__":
    # Test the chunker
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python chunker.py <path/to/file.c>")
        sys.exit(1)
    
    file_path = Path(sys.argv[1])
    chunker = CFileChunker(max_chunk_lines=500, small_file_threshold=800)
    
    if not chunker.should_chunk(file_path):
        print(f"{file_path}: Small file, no chunking needed")
    else:
        chunks = chunker.chunk_file(file_path)
        print(f"{file_path}: Split into {len(chunks)} chunks")
        for i, chunk in enumerate(chunks, 1):
            print(f"  Chunk {i}: {chunk.chunk_type} '{chunk.name}' "
                  f"(lines {chunk.start_line}-{chunk.end_line}, {chunk.line_count} lines)")

