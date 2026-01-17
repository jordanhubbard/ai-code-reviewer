#!/usr/bin/env python3
"""
Language-Aware File Chunker

Splits large files into reviewable chunks based on language-specific boundaries
(functions, classes, etc.). This allows the AI reviewer to handle files of any 
size by reviewing one logical unit at a time while maintaining context.

Supported languages:
- C/C++ (.c, .h, .cpp, .hpp)
- Python (.py)
- Shell scripts (.sh)
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Type


@dataclass
class CodeChunk:
    """Represents a chunk of code to review."""
    file_path: Path
    start_line: int
    end_line: int
    content: str
    chunk_type: str  # 'header', 'function', 'global', 'footer', 'class', 'method'
    name: Optional[str] = None  # Function/class/method name
    
    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


class FileChunker(ABC):
    """Abstract base class for language-specific file chunkers."""
    
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
        Split a file into chunks.
        
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
        
        # Large files: chunk by language-specific boundaries
        return self._chunk_by_structure(file_path, lines)
    
    @abstractmethod
    def _chunk_by_structure(self, file_path: Path, lines: List[str]) -> List[CodeChunk]:
        """Language-specific chunking logic. Must be implemented by subclasses."""
        pass
    
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


class CFileChunker(FileChunker):
    """
    Chunks C source files into reviewable pieces.
    
    Strategy:
    1. Files < threshold: Return as single chunk
    2. Large files: Split by functions
    3. Very large functions: Split into sub-chunks
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
    
    def _chunk_by_structure(self, file_path: Path, lines: List[str]) -> List[CodeChunk]:
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


class PythonChunker(FileChunker):
    """
    Chunks Python source files into reviewable pieces.
    
    Strategy:
    1. Files < threshold: Return as single chunk
    2. Large files: Split by classes and top-level functions
    3. Very large classes/functions: Split into sub-chunks
    """
    
    # Regex to find Python function/class definitions
    FUNCTION_RE = re.compile(
        r'^(?:async\s+)?def\s+(\w+)\s*\(',  # Function definition
        re.MULTILINE
    )
    CLASS_RE = re.compile(
        r'^class\s+(\w+)(?:\([^)]*\))?\s*:',  # Class definition
        re.MULTILINE
    )
    
    def _chunk_by_structure(self, file_path: Path, lines: List[str]) -> List[CodeChunk]:
        """Chunk a file by class and function boundaries."""
        chunks = []
        content = ''.join(lines)
        
        # Find all class and function definitions
        structures = []
        
        # Find classes
        for match in self.CLASS_RE.finditer(content):
            class_name = match.group(1)
            start_pos = match.start()
            line_num = content[:start_pos].count('\n') + 1
            end_line = self._find_indented_block_end(lines, line_num)
            
            structures.append({
                'name': class_name,
                'start_line': line_num,
                'end_line': end_line,
                'type': 'class'
            })
        
        # Find top-level functions (not inside classes)
        for match in self.FUNCTION_RE.finditer(content):
            func_name = match.group(1)
            start_pos = match.start()
            line_num = content[:start_pos].count('\n') + 1
            
            # Check if this function is inside a class
            inside_class = any(
                struct['type'] == 'class' and 
                struct['start_line'] < line_num <= struct['end_line']
                for struct in structures
            )
            
            if not inside_class:
                end_line = self._find_indented_block_end(lines, line_num)
                structures.append({
                    'name': func_name,
                    'start_line': line_num,
                    'end_line': end_line,
                    'type': 'function'
                })
        
        # Sort by start line
        structures.sort(key=lambda x: x['start_line'])
        
        # If no structures found, fall back to line chunking
        if not structures:
            return self._chunk_by_lines(file_path, lines)
        
        # Create header chunk (imports, module docstring, globals before first structure)
        if structures[0]['start_line'] > 1:
            header_content = ''.join(lines[:structures[0]['start_line']-1])
            if header_content.strip():
                chunks.append(CodeChunk(
                    file_path=file_path,
                    start_line=1,
                    end_line=structures[0]['start_line'] - 1,
                    content=header_content,
                    chunk_type='header',
                    name='module_header'
                ))
        
        # Create chunks for each structure
        for i, struct in enumerate(structures):
            struct_lines = lines[struct['start_line']-1:struct['end_line']]
            
            # If structure is huge, split it further
            if len(struct_lines) > self.max_chunk_lines * 2:
                sub_chunks = self._split_large_structure(file_path, struct, struct_lines)
                chunks.extend(sub_chunks)
            else:
                # Include some context between structures
                context_end = struct['end_line']
                if i + 1 < len(structures):
                    next_start = structures[i+1]['start_line']
                    context_lines = min(10, next_start - struct['end_line'] - 1)
                    context_end = struct['end_line'] + context_lines
                
                chunk_content = ''.join(lines[struct['start_line']-1:context_end])
                chunks.append(CodeChunk(
                    file_path=file_path,
                    start_line=struct['start_line'],
                    end_line=context_end,
                    content=chunk_content,
                    chunk_type=struct['type'],
                    name=struct['name']
                ))
        
        # Create footer chunk (code after last structure)
        if structures:
            last_end = structures[-1]['end_line']
            if last_end < len(lines):
                footer_content = ''.join(lines[last_end:])
                if footer_content.strip():
                    chunks.append(CodeChunk(
                        file_path=file_path,
                        start_line=last_end + 1,
                        end_line=len(lines),
                        content=footer_content,
                        chunk_type='footer',
                        name='module_footer'
                    ))
        
        return chunks
    
    def _find_indented_block_end(self, lines: List[str], start_line: int) -> int:
        """
        Find the end of an indented block (class or function).
        
        Args:
            lines: File lines
            start_line: Line number where block starts (1-indexed)
            
        Returns:
            Line number where block ends (1-indexed)
        """
        if start_line > len(lines):
            return len(lines)
        
        # Find the indentation of the definition line
        def_line = lines[start_line - 1]
        base_indent = len(def_line) - len(def_line.lstrip())
        
        # Scan forward to find where indentation returns to base level or less
        for i in range(start_line, len(lines)):
            line = lines[i]
            
            # Skip blank lines and comments
            stripped = line.lstrip()
            if not stripped or stripped.startswith('#'):
                continue
            
            # Check indentation
            current_indent = len(line) - len(stripped)
            if current_indent <= base_indent:
                return i  # Return 1-indexed line number
        
        return len(lines)
    
    def _split_large_structure(
        self, 
        file_path: Path, 
        struct: dict, 
        struct_lines: List[str]
    ) -> List[CodeChunk]:
        """Split a very large class or function into sub-chunks."""
        chunks = []
        num_sub_chunks = (len(struct_lines) + self.max_chunk_lines - 1) // self.max_chunk_lines
        
        for i in range(num_sub_chunks):
            start_idx = i * self.max_chunk_lines
            end_idx = min((i + 1) * self.max_chunk_lines, len(struct_lines))
            
            chunk_content = ''.join(struct_lines[start_idx:end_idx])
            chunks.append(CodeChunk(
                file_path=file_path,
                start_line=struct['start_line'] + start_idx,
                end_line=struct['start_line'] + end_idx - 1,
                content=chunk_content,
                chunk_type=f"{struct['type']}_part",
                name=f"{struct['name']}_part{i+1}"
            ))
        
        return chunks


class ShellScriptChunker(FileChunker):
    """
    Chunks shell script files into reviewable pieces.
    
    Strategy:
    1. Files < threshold: Return as single chunk
    2. Large files: Split by function definitions
    3. Very large functions: Split into sub-chunks
    """
    
    # Regex to find shell function definitions
    FUNCTION_RE = re.compile(
        r'^(?:function\s+)?(\w+)\s*\(\s*\)\s*\{?',  # function name() or function name
        re.MULTILINE
    )
    
    def _chunk_by_structure(self, file_path: Path, lines: List[str]) -> List[CodeChunk]:
        """Chunk a file by shell function boundaries."""
        chunks = []
        content = ''.join(lines)
        
        # Find all function definitions
        functions = []
        for match in self.FUNCTION_RE.finditer(content):
            func_name = match.group(1)
            func_start_pos = match.start()
            line_num = content[:func_start_pos].count('\n') + 1
            
            # Find the function body end
            func_end_line = self._find_function_end(lines, line_num)
            
            functions.append({
                'name': func_name,
                'start_line': line_num,
                'end_line': func_end_line
            })
        
        # If no functions found, fall back to line chunking
        if not functions:
            return self._chunk_by_lines(file_path, lines)
        
        # Create header chunk (shebang, comments, globals before first function)
        if functions[0]['start_line'] > 1:
            header_content = ''.join(lines[:functions[0]['start_line']-1])
            if header_content.strip():
                chunks.append(CodeChunk(
                    file_path=file_path,
                    start_line=1,
                    end_line=functions[0]['start_line'] - 1,
                    content=header_content,
                    chunk_type='header',
                    name='script_header'
                ))
        
        # Create chunks for each function
        for i, func in enumerate(functions):
            func_lines = lines[func['start_line']-1:func['end_line']]
            
            # If function is huge, split it further
            if len(func_lines) > self.max_chunk_lines * 2:
                sub_chunks = self._split_large_function(file_path, func, func_lines)
                chunks.extend(sub_chunks)
            else:
                # Include some context between functions
                context_end = func['end_line']
                if i + 1 < len(functions):
                    next_func_start = functions[i+1]['start_line']
                    context_lines = min(10, next_func_start - func['end_line'] - 1)
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
        
        # Create footer chunk (code after last function)
        if functions:
            last_func_end = functions[-1]['end_line']
            if last_func_end < len(lines):
                footer_content = ''.join(lines[last_func_end:])
                if footer_content.strip():
                    chunks.append(CodeChunk(
                        file_path=file_path,
                        start_line=last_func_end + 1,
                        end_line=len(lines),
                        content=footer_content,
                        chunk_type='footer',
                        name='script_footer'
                    ))
        
        return chunks
    
    def _find_function_end(self, lines: List[str], start_line: int) -> int:
        """
        Find the end of a shell function by matching braces.
        
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
            
            # Count braces (ignore braces in comments and strings - simplified)
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


# Factory function to get appropriate chunker for file type
def get_chunker(file_path: Path, max_chunk_lines: int = 500, 
                small_file_threshold: int = 800) -> FileChunker:
    """
    Get the appropriate chunker for a given file based on its extension.
    
    Args:
        file_path: Path to the file
        max_chunk_lines: Maximum lines per chunk
        small_file_threshold: Files smaller than this won't be chunked
        
    Returns:
        Appropriate FileChunker subclass instance
    """
    suffix = file_path.suffix.lower()
    
    # Map file extensions to chunker classes
    chunker_map: dict[str, Type[FileChunker]] = {
        '.c': CFileChunker,
        '.h': CFileChunker,
        '.cpp': CFileChunker,
        '.hpp': CFileChunker,
        '.cc': CFileChunker,
        '.cxx': CFileChunker,
        '.py': PythonChunker,
        '.sh': ShellScriptChunker,
        '.bash': ShellScriptChunker,
    }
    
    # Get the chunker class or default to CFileChunker for unknown types
    chunker_class = chunker_map.get(suffix, CFileChunker)
    
    return chunker_class(
        max_chunk_lines=max_chunk_lines,
        small_file_threshold=small_file_threshold
    )


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
        print("Usage: python chunker.py <path/to/file>")
        sys.exit(1)
    
    file_path = Path(sys.argv[1])
    chunker = get_chunker(file_path, max_chunk_lines=500, small_file_threshold=800)
    
    print(f"Using chunker: {chunker.__class__.__name__}")
    
    if not chunker.should_chunk(file_path):
        print(f"{file_path}: Small file, no chunking needed")
    else:
        chunks = chunker.chunk_file(file_path)
        print(f"{file_path}: Split into {len(chunks)} chunks")
        for i, chunk in enumerate(chunks, 1):
            print(f"  Chunk {i}: {chunk.chunk_type} '{chunk.name}' "
                  f"(lines {chunk.start_line}-{chunk.end_line}, {chunk.line_count} lines)")

