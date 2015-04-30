#!/usr/bin/python

import argparse
import atexit
import datetime
import difflib
import getpass
import glob
import os
import re
import shlex
import shutil
import StringIO
import subprocess
import sys
import tempfile


script_dir = os.path.dirname(os.path.abspath(__file__))
new_builds = os.path.join(script_dir, "new-builds")
public_dir = os.path.expanduser(
    "~/src/fedora/virt-group-repos/virtio-win/repo-tree")
hosteduser = os.environ.get("FAS_USERNAME", None) or getpass.getuser()


#########################
# specfile helper class #
#########################

class Spec(object):
    """
    Helper class for handling all the spec file editing.
    """

    def __init__(self, origpath, newvirtio, newqxl, newqemuga):
        self.origpath = origpath
        self.origcontent = file(origpath).read()
        self.clognewcontent = ("%changelog" +
            self.origcontent.split("%changelog", 1)[1])

        self.newcontent = file(self.origpath).read()

        self.newvirtio = newvirtio
        self.newqxl = newqxl
        self.newqemuga = newqemuga

        self.origvirtio = self._replace_global("virtio_win_prewhql_build",
            self.newvirtio)
        self.origqxl = self._replace_global("qxl_build", self.newqxl)
        self.origqemuga = self._replace_global("qemu_ga_win_build",
            self.newqemuga)

        self.newrelease, self.newversion = self._set_new_version()
        self._set_new_clog()


    ####################
    # Internal helpers #
    ####################

    def _replace_global(self, pkgname, newvalue):
        patternstub = "%%global %s " % pkgname
        origpattern = patternstub + "([\w\.\d-]+)"
        origvalue = re.findall(origpattern, self.origcontent)[0]
        self.newcontent = re.sub(origpattern, patternstub + newvalue,
            self.newcontent, count=1)
        return origvalue

    def _set_new_version(self):
        version_pattern = "Version: ([\w\.]+)"
        release_pattern = "Release: ([\w\.]+)"
        origrelease = re.findall(release_pattern, self.origcontent)[0]
        origversion = re.findall(version_pattern, self.origcontent)[0]

        newversion = origversion
        newrelease = str(int(origrelease) + 1)

        if self.origvirtio != self.newvirtio:
            newversion = self.newvirtio.split("-", 3)[-1].replace("-", ".")
            newrelease = "1"

        # For Release: explicitly strip out the dist bit, since it's not
        # really relevant for the public RPMs
        self.newcontent = re.sub(release_pattern + ".*\n",
            "Release: %s\n" % newrelease, self.newcontent, count=1)
        self.newcontent = re.sub(version_pattern, "Version: %s" % newversion,
            self.newcontent, count=1)
        return newrelease, newversion

    def _get_final_content(self):
        return self.newcontent + "\n\n" + self.clognewcontent

    def _set_new_clog(self):
        clog = "* %s %s - %s-%s\n" % (
            datetime.datetime.now().strftime("%a %b %d %Y"),
            os.environ["EMAIL"],
            self.newversion, self.newrelease)

        if self.origvirtio != self.newvirtio:
            clog += "- Update to %s\n" % self.newvirtio
        if self.origqxl != self.newqxl:
            clog += "- Update to %s\n" % self.newqxl
        if self.origqemuga != self.newqemuga:
            clog += "- Update to %s\n" % self.newqemuga

        self.clognewcontent = re.sub("%changelog", "%%changelog\n%s" % clog,
            self.clognewcontent).strip() + "\n"


    ##################
    # Public helpers #
    ##################

    def diff(self):
        return "".join(difflib.unified_diff(
            self.origcontent.splitlines(1),
            self._get_final_content().splitlines(1),
            fromfile="Orig spec",
            tofile="New spec"))

    def write_changes(self):
        file(self.origpath, "w").write(self._get_final_content())


#####################
# utility functions #
#####################

def fail(msg):
    print "ERROR: %s" % msg
    sys.exit(1)


def _comm(comm, systemcompat, quiet=False, exc=False, **kwargs):
    try:
        if not quiet:
            print "+ %s" % comm

        output = ""
        read = False
        if systemcompat:
            kwargs["shell"] = True
            if isinstance(sys.stdout, StringIO.StringIO):
                read = True
        else:
            read = True
            if not isinstance(comm, list):
                comm = shlex.split(comm)

        if read:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.STDOUT

        proc = subprocess.Popen(comm, **kwargs)
        try:
            output, ignore = proc.communicate()
            sts = proc.wait()

            if output is not None:
                output = output.strip()
        except (KeyboardInterrupt, SystemExit):
            os.system("stty echo")
            raise

        if read and systemcompat:
            output = output.strip()
            sys.stdout.write(output)

        if sts != 0:
            errmsg = ("Command failed:\ncmd=%s\ncode=%s\nout=\n%s" %
                      (comm, sts, output))
            if exc:
                raise RuntimeError(errmsg)
            fail(errmsg)

        return output, sts
    except Exception, e:
        if exc:
            raise
        fail("Command failed:\n%s\n%s" % (comm, str(e)))


def shellcomm(comm, **kwargs):
    return _comm(comm, True, **kwargs)[1]


def runcomm(comm, **kwargs):
    return _comm(comm, False, **kwargs)[0]


def yes_or_no(msg):
    while 1:
        sys.stdout.write(msg)
        inp = sys.stdin.readline()
        if inp.startswith("y"):
            return True
        return False


######################
# Functional helpers #
######################

def get_package_string(package, zip_dir):
    """
    Find the latest packages by parsing filenames from new_builds
    """
    pattern = os.path.join(zip_dir, package + "*-sources.zip")
    sources_files = glob.glob(pattern)
    if not sources_files:
        fail("Didn't find any matches for %s\n"
            "That directory should contain the downloaded output "
            "from virtio-win-get-latest-builds.py" % pattern)

    return os.path.basename(sources_files[0]).rsplit("-", 1)[0]


def make_virtio_win_rpm_archive(zip_dir, versionstr):
    """
    Call the public virtio-win scripts to organize the driver input for
    the RPM
    """
    input_dir = tempfile.mkdtemp(prefix='virtio-win-input-dir-')
    atexit.register(lambda: shutil.rmtree(input_dir))
    output_dir = tempfile.mkdtemp(prefix='virtio-win-driver-dir-')
    atexit.register(lambda: shutil.rmtree(output_dir))

    # Change virtio-win-prewhql-0.1-100 to virtio-win-0.1.100, since it's
    # what we want for making RPM version happy
    versionstr = (versionstr.rsplit(".", 1)[0] + "." +
        versionstr.rsplit(".", 1)[1].replace("-", ".")).replace("-prewhql", "")

    # Extract contents
    for zipfile in glob.glob(os.path.join(zip_dir, "*.zip")):
        if zipfile.endswith("-sources.zip"):
            continue
        shellcomm("unzip %s -d %s" % (zipfile, input_dir))

    # Build the driver dir
    shellcomm("%s/make-driver-dir.py %s --outdir %s" %
        (script_dir, input_dir, output_dir))

    # Generate archive
    shellcomm("%s/make-virtio-win-rpm-archive.py %s %s" %
        (script_dir, versionstr, output_dir))


def user_edit_clog_content(spec):
    """
    Launch vim and let the user tweak the changelog if they want
    """
    tmp = tempfile.NamedTemporaryFile()
    tmp.write(spec.clognewcontent)
    tmp.flush()
    tmp.seek(0)

    os.system("vim %s" % tmp.name)
    spec.clognewcontent = tmp.read()
    tmp.close()


##################
# main() helpers #
##################

def _build_latest_rpm():
    """
    Extract new-builds/, build the driver dir, build the RPM archive,
    edit the spec, build the RPM, copy it into place
    """
    virtio_str = get_package_string("virtio-win-prewhql", new_builds)
    qxl_str = get_package_string("qxl-win-unsigned", new_builds)
    qemu_ga_str = get_package_string("qemu-ga-win", new_builds)

    # Call public scripts to generate the virtio .zip
    make_virtio_win_rpm_archive(new_builds, virtio_str)

    # Populate RPM dir
    rpm_dir = tempfile.mkdtemp(prefix='virtio-win-rpm-dir-')
    atexit.register(lambda: shutil.rmtree(rpm_dir))

    shellcomm("mv %s/*.zip %s" % (script_dir, rpm_dir))
    shellcomm("cp %s/*-sources.zip %s" % (new_builds, rpm_dir))
    shellcomm("cd %s && mkdir %s && cp *.msi %s && "
        "zip -9 -r %s/%s-installers.zip %s && rm -rf %s" %
        (new_builds, qemu_ga_str, qemu_ga_str, rpm_dir,
         qemu_ga_str, qemu_ga_str, qemu_ga_str))

    # We checkout the rpm_changelog so that subsequent runs don't
    # generate multiple entries.
    os.system("git checkout rpm_changelog")

    # Just creating the Spec will queue up all expected changes.
    spec = Spec(os.path.join(script_dir, "virtio-win.spec"),
        virtio_str, qxl_str, qemu_ga_str)

    # Confirm with the user that everything looks good
    while True:
        print spec.diff()
        print
        if yes_or_no("Use this spec diff? (y/n, 'n' to edit changelog): "):
            break

        os.system("clear")
        user_edit_clog_content(spec)
        os.system("clear")

    # Save the changes
    spec.write_changes()
    newspecpath = os.path.join(rpm_dir, os.path.basename(spec.origpath))
    shutil.copy2(spec.origpath, newspecpath)

    # Build the RPM
    shellcomm("cd %s && rpmbuild -ba %s" %
        (rpm_dir, os.path.basename(newspecpath)))

    return glob.glob(os.path.join(rpm_dir, "*.rpm"))


def _copy_rpms_to_local_tree(rpms):
    """
    Copy RPMs to our local tree mirror, to get ready for repo creation
    """
    print
    print
    for path in rpms:
        filename = os.path.basename(path)
        if filename.endswith(".src.rpm"):
            dest = os.path.join(public_dir, "srpms", filename)
        else:
            dest = os.path.join(public_dir, "rpms", filename)

        shutil.move(path, dest)
        print "Generated %s" % dest


def _generate_repos():
    """
    Run createrepo
    """
    for rpmdir in ["rpms", "stable", "srpms"]:
        shellcomm("rm -rf %s" %
            os.path.join(public_dir, rpmdir, "repodata"))
        shellcomm("createrepo %s > /dev/null" %
            os.path.join(public_dir, rpmdir))


def _push_repos():
    """
    rsync the changes to fedorapeople.org
    """
    if not yes_or_no("rsync the changes to fedorapeople? (y/n): "):
        return

    # Put the RPMs in place
    prog = (sys.stdin.isatty() and "--progress " or " ")
    shellcomm("rsync -avz %s --exclude repodata %s/ "
        "%s@fedorapeople.org:~/virtgroup/virtio-win/repo" %
        (prog, public_dir, hosteduser))

    # Overwrite the repodata and remove stale files
    shellcomm("rsync -avz %s --delete %s/ "
        "%s@fedorapeople.org:~/virtgroup/virtio-win/repo" %
        (prog, public_dir, hosteduser))


###################
# main() handling #
###################

def parse_args():
    parser = argparse.ArgumentParser(description="Scoop up the downloaded "
        "builds from new_builds, generate the RPM using the public scripts "
        "and drop the output in $CWD.")

    parser.add_argument("--repo-only", action="store_true",
        help="Only regenerate repo and push changes")

    return parser.parse_args()


def main():
    options = parse_args()
    ignore = options

    if not options.repo_only:
        rpms = _build_latest_rpm()
        _copy_rpms_to_local_tree(rpms)
        shutil.rmtree(new_builds)

    _generate_repos()
    _push_repos()

    # Inform user about manual tasks
    print "\n"
    print "Don't forget to:"
    print "- Commit all the spec file changes"
    print "- If this is a stable build, symlink it into stable/ and "
    print "  regenerate the repos."
    print

    return 0


if __name__ == '__main__':
    sys.exit(main())
