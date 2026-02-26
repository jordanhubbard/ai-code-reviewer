# Contributing to AI Code Reviewer

Thank you for your interest in contributing!

## Ways to Contribute

### 1. Bug Reports

Found a bug? [Open an issue](https://github.com/jordanhubbard/ai-code-reviewer/issues) with:
- What you expected
- What actually happened
- Steps to reproduce
- Your config.yaml (redact sensitive info)

### 2. Feature Requests

Have an idea? [Open an issue](https://github.com/jordanhubbard/ai-code-reviewer/issues) with:
- Use case description
- Why it's valuable
- Proposed approach (optional)

### 3. Code Contributions

#### Quick Start

1. Fork the repository
2. Create a branch: `git checkout -b feature/my-feature`
3. Make your changes
4. Test thoroughly
5. Commit: `git commit -m "Add: my feature"`
6. Push: `git push origin feature/my-feature`
7. Open a Pull Request

#### Guidelines

- **Code style**: Follow existing patterns
- **Documentation**: Update docs for user-facing changes
- **Testing**: Test on multiple projects/languages
- **Commits**: Clear, descriptive messages

### 4. Persona Contributions

Created a useful persona? Share it!

1. Create `personas/your-persona/`
2. Include all required files (see personas/example/README.md)
3. Add a descriptive README.md
4. Open a PR

Popular personas may be included in the repo.

### 5. Documentation

- Fix typos
- Clarify confusing sections
- Add examples
- Write tutorials

## Development Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/ai-code-reviewer.git
cd ai-code-reviewer

# Install dependencies
pip install -r requirements.txt

# Test on a small project
cp config.yaml.sample config.yaml
vim config.yaml  # Configure for your test project
python reviewer.py
```

## Testing

Before submitting a PR:

1. **Lint**: Run `make check-deps` to verify dependencies
2. **Test**: Run on a small project completely
3. **Verify**: Check that:
   - Source tree stays clean
   - Persona files update correctly
   - Build integration works
   - Progress tracking works

## Code Architecture

Key files:
- **reviewer.py**: Main review loop
- **llm_client.py**: LLM communication (any OpenAI-compatible provider)
- **build_executor.py**: Build/test execution
- **chunker.py**: Large file handling
- **index_generator.py**: Directory scanning

See README.md for architecture diagram.

## Pull Request Process

1. **Description**: Explain what and why
2. **Testing**: Describe how you tested
3. **Documentation**: Update relevant docs
4. **Review**: Address feedback promptly

## Style Guide

- Python: Follow PEP 8 generally, but match existing code style
- Comments: Explain WHY, not WHAT
- Functions: Do one thing well
- Errors: Helpful messages

## Questions?

- Check existing issues/discussions
- Open an issue for questions
- Tag with "question" label

## License

By contributing, you agree your contributions will be licensed under BSD 2-Clause.

## Code of Conduct

Be respectful and constructive. We're all here to make code reviews better!

## Recognition

Contributors are recognized in:
- Git commit history
- Release notes
- Project README (for significant contributions)

Thank you for making AI code review better! 🚀

