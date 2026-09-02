# 📋 Metadata Update Process Guide

## 🎯 Purpose
This document explains how metadata is automatically maintained in this repository and provides fallback instructions for manual updates if needed.

---

## ✨ Automated Process (Recommended)

### 🤖 Git Pre-Commit Hook

**The repository now includes automated metadata synchronization!** All metadata files are automatically updated when you commit review files.

### How It Works

1. **Add a new review file:**
```bash
# Create your review (Hebrew or English)
vim mike-paper-reviews-all/split-hebrew-reviews-md/Review_XXX.md
```

2. **Include paper link in the review:**
```markdown
המאמר היומי של מייק: DD.MM.YY
Review XXX: Paper Title

Review content here...

https://arxiv.org/abs/XXXX.XXXXX
```

3. **Commit (hook runs automatically):**
```bash
git add mike-paper-reviews-all/split-hebrew-reviews-md/Review_XXX.md
git commit -m "Add Review_XXX: Paper Title"

# Output:
# 📝 Detected review markdown changes, updating metadata...
# Extracting metadata from Hebrew review files...
# Extracted 573 reviews
# Reviews with missing links: 0
# ✓ Metadata updated successfully
# ✓ Metadata files staged for commit
```

4. **Push to remote:**
```bash
git push
```

### What Gets Auto-Updated

The pre-commit hook automatically updates:

| File | Purpose | Location |
|------|---------|----------|
| `paper_with_links.csv` | Review number → Paper title → Link | `reviews_metadata/` |
| `all_paper_titles.txt` | Numbered list of all titles | `reviews_metadata/` |
| `clean_titles_for_search.txt` | Sanitized titles for search | `reviews_metadata/` |
| `reviews_from_208_titles.txt` | Titles for reviews 208+ | `reviews_metadata/` |

### Supported Paper Sources

The hook extracts links from multiple sources:
- **ArXiv** - `https://arxiv.org/abs/XXXX.XXXXX`
- **Nature** - `https://nature.com/articles/...`
- **ACM Digital Library** - `https://dl.acm.org/doi/...`
- **OpenAI** - `https://openai.com/...` or `https://cdn.openai.com/papers/...`
- **Google Research** - `https://research.google/blog/...`
- **OpenReview** - `https://openreview.net/forum?id=...`
- **HuggingFace Papers** - `https://huggingface.co/papers/...`
- **DOI Links** - `https://doi.org/...`
- **ScienceDirect** - `https://sciencedirect.com/...`
- **Research Square** - `https://researchsquare.com/...`

### Title Extraction Patterns

The hook recognizes multiple review formats:

```markdown
# Pattern 1: Standard header
Review XXX: Paper Title

# Pattern 2: Hebrew date format
המאמר היומי של מייק: DD.MM.YYPaper Title

# Pattern 3: Mixed Hebrew/English
סקירה XXX סקירות עד 1024 Paper Title
```

### Link Extraction

The hook handles various link formats:
- Missing protocol: `arxiv.org/abs/...` → `https://arxiv.org/abs/...`
- Single slash typo: `https:/arxiv.org` → `https://arxiv.org`
- PDF to abs: `https://arxiv.org/pdf/...` → `https://arxiv.org/abs/...`
- Normalizes all links to standard format

---

## 📊 Current Statistics

| Metric | Value |
|--------|-------|
| **Total Reviews** | 612 |
| **With Paper Links** | 610 (100% coverage!) |
| **Hebrew Reviews** | 612 markdown files |
| **English Reviews** | 245 markdown files |
| **DOCX Source Files** | 612 files |

---

## 🔧 Manual Process (Fallback)

### When to Use Manual Updates

Use manual updates only if:
- The git hook is not installed or malfunctioning
- You need to fix a specific metadata entry
- You're batch-updating historical reviews

### Manual Update Steps

#### Step 1: Run the Standalone Updater

```bash
cd /Users/mike_erlihson/personal/repos/scientific-resources
python3 .repo-tools/scripts/update_metadata.py
```

This script:
- Scans all review markdown files
- Extracts titles and paper links
- Updates all 4 metadata files
- Reports any missing links

#### Step 2: Verify Updates

```bash
# Check total count
wc -l mike-paper-reviews-all/reviews_metadata/paper_with_links.csv
# Should show: 613 (1 header + 612 reviews)

# Check for missing links
grep ",,$" mike-paper-reviews-all/reviews_metadata/paper_with_links.csv
# Should return nothing if all reviews have links

# Verify last entry
tail -5 mike-paper-reviews-all/reviews_metadata/all_paper_titles.txt
```

#### Step 3: Commit Changes

```bash
git add mike-paper-reviews-all/reviews_metadata/*.{csv,txt}
git commit -m "Manual metadata update: Reviews X-Y"
git push
```

---

## 🐛 Troubleshooting

### Hook Not Running

If the hook doesn't run automatically:

```bash
# Check if hook exists and is executable
ls -la .git/hooks/pre-commit

# Make it executable if needed
chmod +x .git/hooks/pre-commit

# Test the hook manually
.git/hooks/pre-commit
```

### Missing Paper Link

If a review doesn't have a paper link:

1. **Add link to the review markdown file** (recommended):
```bash
vim mike-paper-reviews-all/split-hebrew-reviews-md/Review_XXX.md
# Add the arxiv/doi link at the end
```

2. **Or manually edit CSV** (not recommended):
```bash
vim mike-paper-reviews-all/reviews_metadata/paper_with_links.csv
# Add: Review_XXX,Paper Title,https://arxiv.org/abs/...
```

Then commit:
```bash
git add mike-paper-reviews-all/split-hebrew-reviews-md/Review_XXX.md
git commit -m "Add missing link for Review_XXX"
# Hook will re-extract and update metadata
```

### Title Extraction Failed

If the hook can't extract a title:

1. Check the review file format
2. Ensure the title is in English (or mostly ASCII)
3. The title should appear in the first 15 lines
4. Use one of the supported format patterns (see above)

### Duplicate Entries

If you see duplicate entries in metadata:

```bash
# Check for duplicates
sort mike-paper-reviews-all/reviews_metadata/all_paper_titles.txt | uniq -d

# Re-run the standalone updater to fix
python3 .repo-tools/scripts/update_metadata.py
```

---

## 📝 Quality Assurance

### Verification Checklist

After updates (automatic or manual), verify:

- [ ] All metadata files updated: `paper_with_links.csv`, `all_paper_titles.txt`, `clean_titles_for_search.txt`, `reviews_from_208_titles.txt`
- [ ] Total count matches in all files (612 reviews)
- [ ] No duplicate entries
- [ ] All links are working and properly formatted
- [ ] Sequential numbering with no gaps (Review_001 to Review_612)
- [ ] No empty link fields (100% coverage)

### Quick Verification Commands

```bash
# Count reviews in each file
echo "CSV entries:" && tail -n +2 mike-paper-reviews-all/reviews_metadata/paper_with_links.csv | wc -l
echo "All titles:" && wc -l mike-paper-reviews-all/reviews_metadata/all_paper_titles.txt
echo "Clean titles:" && wc -l mike-paper-reviews-all/reviews_metadata/clean_titles_for_search.txt

# Check for missing links
echo "Missing links:" && grep ",,$" mike-paper-reviews-all/reviews_metadata/paper_with_links.csv | wc -l

# Verify markdown files
echo "Hebrew reviews:" && ls mike-paper-reviews-all/split-hebrew-reviews-md/Review_*.md | wc -l
echo "DOCX files:" && ls mike-paper-reviews-all/split-reviews-docx/Review_*.docx | wc -l
```

---

## 🚀 Best Practices

### For Adding Reviews

1. ✅ **Use the automated workflow** (git hook)
2. ✅ **Include paper link in the review text** (not just in commit message)
3. ✅ **Follow naming convention**: `Review_XXX.md` (zero-padded, e.g., `Review_073.md`)
4. ✅ **Use standard link format** (https:// prefix, standard domain)
5. ✅ **Commit with descriptive message**: `"Add Review_XXX: Paper Title"`

### For Maintaining Metadata

1. ✅ **Trust the automation** - Don't manually edit CSV files
2. ✅ **Fix source files** - If metadata is wrong, fix the review markdown file
3. ✅ **Let the hook re-extract** - Commit the fixed review file
4. ✅ **Verify after commits** - Check that metadata updated correctly
5. ✅ **Keep paper links in reviews** - Ensures long-term maintainability

### For Edge Cases

1. ⚠️ **Non-arxiv papers** - Hook supports 10+ sources, just include the link
2. ⚠️ **No paper link available** - Leave link field empty, hook will note it
3. ⚠️ **Multiple links** - Hook extracts first valid paper link found
4. ⚠️ **Historical reviews** - Can be batch-processed with standalone script

---

## 📅 Maintenance Schedule

| Task | Frequency | Description |
|------|-----------|-------------|
| **Add new reviews** | As needed | Automated via git hook |
| **Verify metadata** | Monthly | Run verification commands |
| **Update README** | Quarterly | Update statistics and coverage dates |
| **Audit links** | Annually | Check for broken links |

---

## 🔄 Migration Notes

### From Manual to Automated (February 2026)

The repository migrated from manual metadata updates to automated git hooks:

**Before (Manual):**
- Extract titles with Python scripts
- Manually edit 4 metadata files
- Verify consistency manually
- 30+ minutes per batch

**After (Automated):**
- Just commit review files
- Hook auto-extracts and updates
- 100% consistency guaranteed
- < 1 minute per review

**Migration Actions Taken:**
1. ✅ Created git pre-commit hook
2. ✅ Created standalone Python updater
3. ✅ Updated all metadata to 100% coverage
4. ✅ Fixed all arxiv link typos
5. ✅ Added support for non-arxiv sources
6. ✅ Documented automation in README

---

## 📚 Additional Resources

- **Git Hook Location**: `.git/hooks/pre-commit`
- **Standalone Updater**: `.repo-tools/scripts/update_metadata.py`
- **Main Updater Class**: `.repo-tools/repo_automator/updaters/metadata_updater.py`
- **README Documentation**: See "🤖 Automated Metadata Updates" section

---

**Last Updated:** September 2, 2026
**Automation Status:** ✅ Fully Automated via Git Hook
**Coverage:** 612/612 reviews (100%)
**Repository:** https://github.com/merlihson/scientific-resources
