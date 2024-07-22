from base import *
from utils.logger import *
from utils.download import download_and_extract, parse_repo_info

logger = get_logger()

def get_latest_release():
    try:
        try:
            username, repository, branch = parse_repo_info('PD_REPO')
        except ValueError as e:
            logger.error(str(e))
            return False, str(e)

        repo_url = f"https://github.com/{username}/{repository}/archive/refs/heads/{branch}.zip"
        logger.debug(f"Repository URL for plex_debrid download: {repo_url}")  

        target = './plex_debrid'
        
        success, error = download_and_extract(repo_url, target)
        if not success:
            logger.error(f"Error downloading latest plex_debrid release: {error}")
            return False, error
        
        return True, None
    except Exception as e:
        logger.error(f"Error downloading latest plex_debrid release: {e}")
        return False, str(e)
