# -*- coding: utf-8 -*-

import textwrap
from collections import defaultdict
from itertools import chain, combinations, filterfalse, product
from functools import partial, lru_cache
from operator import itemgetter

from snakemake.io import IOFile, _IOFile
from snakemake.jobs import Job, Reason
from snakemake.exceptions import RuleException, MissingInputException
from snakemake.exceptions import MissingRuleException, AmbiguousRuleException
from snakemake.exceptions import CyclicGraphException, MissingOutputException
from snakemake.logging import logger

__author__ = "Johannes Köster"


class DAG:
    def __init__(
        self,
        workflow,
        targetfiles=None,
        targetrules=None,
        forceall=False,
        forcetargets=False,
        forcerules=None,
        priorityfiles=None,
        priorityrules=None,
        ignore_ambiguity=False):

        self.dependencies = defaultdict(partial(defaultdict, set))
        self.depending = defaultdict(partial(defaultdict, set))
        self._needrun = set()
        self._reason = defaultdict(Reason)
        self._finished = set()
        self._dynamic = set()
        self._len = 0
        self.workflow = workflow
        self.rules = set(workflow.rules)
        self.ignore_ambiguity = ignore_ambiguity
        self.targetfiles = targetfiles
        self.targetrules = targetrules
        self.priorityfiles = priorityfiles
        self.priorityrules = priorityrules
        self.targetjobs = set()
        self.prioritytargetjobs = set()
        self._ready_jobs = set()

        self.forcerules = set()
        self.forcefiles = set()
        if forceall:
            self.forcerules.update(self.rules)
        elif forcerules:
            self.forcerules.update(forcerules)
        if forcetargets:
            self.forcerules.update(targetrules)
            self.forcefiles.update(targetfiles)
        self.omitforce = set()

    def init(self):
        """ Initialise the DAG. """
        for job in map(self.rule2job, self.targetrules):
            job = self.update([job])
            self.targetjobs.add(job)

        exceptions = defaultdict(list)
        for file in self.targetfiles:
            try:
                job = self.update(self.file2jobs(file), file=file)
                self.targetjobs.add(job)
            except MissingRuleException as ex:
                exceptions[file].append(ex)

        if exceptions:
            raise RuleException(include=chain(*exceptions.values()))
        self.update_needrun()

        for job in filter(
            lambda job: (job.dynamic_output
                and not self.needrun(job)), self.jobs):
            self.update_dynamic(job)
        self.postprocess()

    @property
    def jobs(self):
        """ All jobs in the DAG. """
        for job in self.bfs(self.dependencies, *self.targetjobs):
            yield job

    @property
    def needrun_jobs(self):
        """ Jobs that need to be executed. """
        for job in filter(self.needrun, self.bfs(
            self.dependencies, *self.targetjobs, stop=self.finished)):
            yield job

    @property
    def ready_jobs(self):
        """ Jobs that are ready to execute. """
        return self._ready_jobs

    def ready(self, job):
        """ Return whether a given job is ready to execute. """
        return job in self._ready_jobs

    def needrun(self, job):
        """ Return whether a given job is needs to be executed. """
        return job in self._needrun

    def noneedrun_finished(self, job):
        """
        Return whether a given job is finished or was not
        required to run at all.
        """
        return not self.needrun(job) or self.finished(job)

    def reason(self, job):
        """ Return the reason of the job execution. """
        return self._reason[job]

    def finished(self, job):
        """ Return whether a job is finished. """
        return job in self._finished

    def dynamic(self, job):
        """
        Return whether a job is dynamic (i.e. it is only a placeholder
        for those that are created after the job with dynamic output has
        finished.
        """
        return job in self._dynamic

    def requested_files(self, job):
        """ Return the files a job requests. """
        return set(*self.depending[job].values())

    def missing_temp(self, job):
        """
        Return whether a temp file that is input of the given job is missing.
        """
        for job_, files in self.depending[job].items():
            if self.needrun(job_) and any(not f.exists for f in files):
                return True
        return False

    def check_output(self, job):
        """ Raise exception if output files of job are missing. """
        for f in job.expanded_output:
            if not f.exists:
                raise MissingOutputException("Output file {} not "
                    "produced by rule {}.".format(f, job.rule.name),
                    lineno=job.rule.lineno, snakefile=job.rule.snakefile)

    def handle_protected(self, job):
        """ Write-protect output files that are marked with protected(). """
        for f in job.expanded_output:
            if f in job.protected_output:
                logger.warning("Write protecting output file {}".format(f))
                f.protect()

    def handle_temp(self, job):
        """ Remove temp files if they are no longer needed. """
        needed = lambda job_, f: any(f in files
            for j, files in self.depending[job_].items()
            if not self.finished(j) and j != job)
        for job_, files in self.dependencies[job].items():
            for f in job_.temp_output & files:
                if not needed(job_, f):
                    logger.warning("Removing temporary "
                        "output file {}".format(f))
                    f.remove()

    def update(self, jobs, file=None, visited=None, skip_until_dynamic=False):
        """ Update the DAG by adding given jobs and their dependencies. """
        if visited is None:
            visited = set()
        producer = None
        exceptions = list()
        jobs = sorted(jobs, reverse=not self.ignore_ambiguity)
        cycles = list()
        for i, job in enumerate(jobs):
            if file in job.input:
                cycles.append(job)
                continue
            if job in visited:
                cycles.append(job)
                continue
            try:
                self.update_(
                    job, visited=set(visited),
                    skip_until_dynamic=skip_until_dynamic)
                if i > 0:
                    if job < jobs[i - 1] or self.ignore_ambiguity:
                        break
                    elif producer is not None:
                        raise AmbiguousRuleException(
                            file, job.rule, jobs[i - 1].rule)
                producer = job
            except (MissingInputException, CyclicGraphException) as ex:
                exceptions.append(ex)
            except RuntimeError as ex:
                if (isinstance(ex, RuntimeError) and
                    str(ex).startswith("maximum recursion depth exceeded")):
                    ex = RuleException("Maximum recursion depth exceeded. "
                        "Maybe you have a cyclic dependency due to infinitely "
                        "filled wildcards?\nProblematic "
                        "input file:\n{}".format(file), rule=job.rule)
                raise ex
        if producer is None:
            if cycles:
                job = cycles[0]
                raise CyclicGraphException(job.rule, file, rule=job.rule)
            if exceptions:
                raise exceptions[0]
        return producer

    def update_(self, job, visited=None, skip_until_dynamic=False):
        """ Update the DAG by adding the given job and its dependencies. """
        if job in self.dependencies:
            return
        if visited is None:
            visited = set()
        visited.add(job)
        dependencies = self.dependencies[job]
        potential_dependencies = self.collect_potential_dependencies(
            job).items()

        skip_until_dynamic = skip_until_dynamic and not job.dynamic_output

        producer = dict()
        exceptions = dict()
        for file, jobs in potential_dependencies:
            # TODO check for pumping up wildcards...
            try:
                producer[file] = self.update(jobs, file=file, visited=visited,
                    skip_until_dynamic=skip_until_dynamic
                        or file in job.dynamic_input)
            except (MissingInputException, CyclicGraphException) as ex:
                exceptions[file] = ex

        for file, job_ in producer.items():
            dependencies[job_].add(file)
            self.depending[job_][job].add(file)

        missing_input = job.missing_input - set(producer)
        if missing_input:
            include = list()
            noproducer = list()
            for f in missing_input:
                if f in exceptions:
                    include.append(exceptions[f])
                else:
                    noproducer.append(f)
            self.delete_job(job, recursive=False)  # delete job from tree
            raise MissingInputException(job.rule, noproducer, include=include)

        if skip_until_dynamic:
            self._dynamic.add(job)

    def update_needrun(self):
        """ Update the information whether a job needs to be executed. """
        def output_mintime(job):
            for job_ in self.bfs(self.depending, job):
                t = job_.output_mintime
                if t:
                    return t

        def needrun(job):
            reason = self.reason(job)
            if (job not in self.omitforce and job.rule in self.forcerules
                or not self.forcefiles.isdisjoint(job.output)):
                reason.forced = True
            elif job in self.targetjobs:
                if not job.output:
                    if job.input:
                        reason.updated_input_run.update(
                            [f for f in job.input if not f.exists])
                    else:
                        reason.noio = True
                else:
                    if job.rule in self.targetrules:
                        missing_output = job.missing_output()
                    else:
                        missing_output = job.missing_output(
                            requested=set(chain(*self.depending[job].values()))
                                | self.targetfiles)
                    reason.missing_output.update(missing_output)
            if not reason:
                output_mintime_ = output_mintime(job)
                if output_mintime_:
                    updated_input = [f for f in job.input
                        if f.exists and f.is_newer(output_mintime_)]
                    reason.updated_input.update(updated_input)
            return job

        queue = list(filter(self.reason, map(needrun, self.jobs)))
        visited = set(queue)
        while queue:
            job = queue.pop(0)
            self._needrun.add(job)

            for job_, files in self.dependencies[job].items():
                missing_output = job_.missing_output(requested=files)
                incomplete_output = (job_.output
                    if self.workflow.persistence.incomplete(job_) else set())
                self.reason(job_).missing_output.update(missing_output)
                self.reason(job_).incomplete_output.update(incomplete_output)
                if (missing_output
                    or incomplete_output) and not job_ in visited:
                    visited.add(job_)
                    queue.append(job_)

            for job_, files in self.depending[job].items():
                self.reason(job_).updated_input_run.update(files)
                if not job_ in visited:
                    visited.add(job_)
                    queue.append(job_)

        self._len = len(self._needrun)

    def update_priority(self):
        """ Update job priorities. """
        prioritized = (lambda job: job.rule in self.priorityrules
            or not self.priorityfiles.isdisjoint(job.output))
        for job in self.bfs(
            self.dependencies, *filter(prioritized, self.needrun_jobs),
            stop=self.noneedrun_finished):
            job.priority = Job.HIGHEST_PRIORITY

    def update_ready(self):
        """ Update information whether a job is ready to execute. """
        for job in filter(self.needrun, self.jobs):
            if not self.finished(job) and self._ready(job):
                self._ready_jobs.add(job)

    def postprocess(self):
        self.update_needrun()
        self.update_priority()
        self.update_ready()

    def _ready(self, job):
        return self._finished.issuperset(
            filter(self.needrun, self.dependencies[job]))

    def finish(self, job, update_dynamic=True):
        self._finished.add(job)
        self._ready_jobs.remove(job)
        # mark depending jobs as ready
        for job_ in self.depending[job]:
            if self._ready(job_):
                self._ready_jobs.add(job_)

        if update_dynamic and job.dynamic_output:
            logger.warning("Dynamically updating jobs")
            newjob = self.update_dynamic(job)
            if newjob:
                # simulate that this job ran and was finished before
                self.omitforce.add(newjob)
                self._needrun.add(newjob)
                self._finished.add(newjob)

                self.postprocess()

    def update_dynamic(self, job):
        dynamic_wildcards = job.dynamic_wildcards
        if not dynamic_wildcards:
            # this happens e.g. in dryrun if output is not yet present
            return

        depending = list(
            filter(lambda job_: not self.finished(job_),
                self.bfs(self.depending, job)))
        newrule, non_dynamic_wildcards = job.rule.dynamic_branch(
            dynamic_wildcards, input=False)
        self.replace_rule(job.rule, newrule)

        # no targetfile needed for job
        newjob = Job(
            newrule, format_wildcards=non_dynamic_wildcards)
        self.replace_job(job, newjob)
        for job_ in depending:
            if job_.dynamic_input:
                newrule_ = job_.rule.dynamic_branch(dynamic_wildcards)
                if newrule_ is not None:
                    self.replace_rule(job_.rule, newrule_)
                    if not self.dynamic(job_):
                        newjob_ = Job(newrule_, targetfile=job_.targetfile)
                        self.replace_job(job_, newjob_)
        return newjob

    def delete_job(self, job, recursive=True):
        for job_ in self.depending[job]:
            del self.dependencies[job_][job]
        del self.depending[job]
        for job_ in self.dependencies[job]:
            depending = self.depending[job_]
            del depending[job]
            if not depending and recursive:
                self.delete_job(job_)
        del self.dependencies[job]
        if job in self._needrun:
            self._len -= 1
            self._needrun.remove(job)
            del self._reason[job]
        if job in self._finished:
            self._finished.remove(job)
        if job in self._dynamic:
            self._dynamic.remove(job)
        if job in self._ready_jobs:
            self._ready_jobs.remove(job)

    def replace_job(self, job, newjob):
        depending = list(self.depending[job].items())
        if self.finished(job):
            self._finished.add(newjob)
        self.delete_job(job)
        self.update([newjob])
        for job_, files in depending:
            if not job_.dynamic_input:
                self.dependencies[job_][newjob].update(files)
                self.depending[newjob][job_].update(files)
        if job in self.targetjobs:
            self.targetjobs.remove(job)
            self.targetjobs.add(newjob)

    def replace_rule(self, rule, newrule):
        assert newrule is not None
        try:
            self.rules.remove(rule)
        except KeyError:
            pass  # ignore if rule was already removed
        self.rules.add(newrule)
        if rule in self.forcerules:
            self.forcerules.add(newrule)

    def collect_potential_dependencies(self, job):
        dependencies = defaultdict(list)
        # use a set to circumvent multiple jobs for the same file
        # if user specified it twice
        for file in set(job.input):
            try:
                for job_ in self.file2jobs(file):
                    dependencies[file].append(job_)
            except MissingRuleException as ex:
                pass
        return dependencies

    def bfs(self, direction, *jobs, stop=lambda job: False):
        queue = list(jobs)
        visited = set(queue)
        while queue:
            job = queue.pop(0)
            if stop(job):
                # stop criterion reached for this node
                continue
            yield job
            for job_, _ in direction[job].items():
                if not job_ in visited:
                    queue.append(job_)
                    visited.add(job_)

    def dfs(self, direction, *jobs, stop=lambda job: False, post=True):
        visited = set()
        for job in jobs:
            for job_ in self._dfs(
                direction, job, visited, stop=stop, post=post):
                yield job_

    def _dfs(self, direction, job, visited, stop, post):
        if stop(job):
            return
        if not post:
            yield job
        for job_ in direction[job]:
            if not job_ in visited:
                visited.add(job_)
                for j in self._dfs(direction, job_, visited, stop, post):
                    yield j
        if post:
            yield job

    def new_wildcards(self, job):
        new_wildcards = set(job.wildcards.items())
        for job_ in self.dependencies[job]:
            if not new_wildcards:
                return set()
            for wildcard in job_.wildcards.items():
                new_wildcards.discard(wildcard)
        return new_wildcards

    def rule2job(self, targetrule):
        return Job(rule=targetrule)

    def file2jobs(self, targetfile):
        jobs = list()
#        print("---")
        for rule in self.rules:
            if rule.is_producer(targetfile):
#                print(rule)
                jobs.append(Job(rule, targetfile=targetfile))
        if not jobs:
            raise MissingRuleException(targetfile)
        return jobs

    def dot(self, errors=False):
        huefactor = 2 / (3 * (len(self.rules)-1))
        rulecolor = dict(
            (rule, "{} 0.6 0.85".format(i * huefactor))
            for i, rule in enumerate(self.rules))

        jobid = dict((job, i) for i, job in enumerate(self.jobs))

        nodes, edges = list(), list()
        types = ["running job", "not running job", "dynamic job"]
        styles = [
            'style="rounded"', 'style="rounded,dashed"',
            'style="rounded,dotted"']
        used_types = set()

        def format_wildcard(wildcard):
            name, value = wildcard
            if _IOFile.dynamic_fill in value:
                value = "..."
            return "{}: {}".format(name, value)

        for job in self.jobs:
            label = "\\n".join([job.rule.name] + list(
                map(format_wildcard, self.new_wildcards(job))))
            t = 0
            if not self.needrun(job):
                t = 1
            if self.dynamic(job) or job.dynamic_input:
                t = 2
            used_types.add(t)

            nodes.append('\t{}[label = "{}", color="{}", {}];'.format(
                jobid[job], label, rulecolor[job.rule], styles[t]))

            for job_ in self.dependencies[job]:
                edges.append("\t{} -> {};".format(jobid[job_], jobid[job]))
        legend = list()
        if len(used_types) > 1:
            for t in used_types:
                legend.append('\tlegend{}[label="{}", {}];'.format(
                    t, types[t], styles[t]))
                for target in map(jobid.__getitem__, self.targetjobs):
                    legend.append(
                        "\t{} -> legend{}[style=invis];".format(target, t))

        return textwrap.dedent("""\
                            digraph snakemake_dag {{
                                graph[bgcolor=white];
                                node[shape=box,style=rounded, fontname=sans, fontsize=10, penwidth=2];
                                edge[penwidth=2, color=grey];
                            {nodes}
                            {edges}
                            {legend}
                            }}\
                            """).format(nodes="\n".join(nodes),
                                        edges="\n".join(edges),
                                        legend="\n".join(legend))

    def d3sankey(self):
        """
        Experiment: plot the DAG as sankey diagram
        (does not yet work for large DAGs.
        """

        def node(job):
            return textwrap.dedent("""
            {{
                "name": "{name}"
            }}
            """).format(name=job.rule.name)

        def edge(indices, value):
            return textwrap.dedent("""
            {{
                "source": {},
                "target": {},
                "value": 1
            }}
            """).format(*indices)
        jobs = list(self.jobs)
        jobindex = dict((job, k) for k, job in enumerate(jobs))

        def edges():
            for job in jobs:
                for p, files in self.dependencies[job].items():
                    yield edge((jobindex[p], jobindex[job]), len(files))

        nodes = "[{}]".format(",".join(map(node, jobs)))
        edges = "[{}]".format(",".join(edges()))
        height = max(300, len(jobs) * 10)
        width = max(800, len(jobs) * 20)

        html = textwrap.dedent(r"""
        <html>
        <head>
        <meta charset="utf-8">
        <style>
        body {{
          font-family: sans-serif;
          font-size: small;
        }}

        .node rect {{
          cursor: move;
          fill-opacity: .9;
          shape-rendering: crispEdges;
        }}

        .node text {{
          pointer-events: none;
          text-shadow: 0 1px 0 #fff;
        }}

        .link {{
          fill: none;
          stroke: #000;
          stroke-opacity: .2;
        }}

        .link:hover {{
          stroke-opacity: .5;
        }}
        </style>
        </head>
        <body>
        <script src="http://d3js.org/d3.v3.min.js"></script>
        <script src="https://raw.github.com/d3/d3-plugins/master/sankey/sankey.js"></script>
        <script>
        var nodes = {nodes};
        var links = {links};

        var width = {width},
            height = {height};
        var color = d3.scale.category20();

        var svg = d3.select("body").append("svg")
            .attr("width", width)
            .attr("height", height);
            //.append("g").attr("transform", "translate(40,0)");

        var layout = d3.sankey()
            .size([width, height])
            .nodeWidth(15)
            .nodePadding(10)
            .nodes(nodes)
            .links(links)
            .layout(32);
        var path = layout.link();

        var link = svg.append("g").selectAll(".link")
                .data(links)
            .enter().append("path")
                .attr("class", "link")
                .attr("d", path)
                .style("stroke-width", function(d) {{ return Math.max(1, d.dy); }})
                .sort(function(a, b) {{ return b.dy - a.dy; }});

        link.append("title")
            .text(function(d) {{ return d.source.name + " → " + d.target.name + "\n" + d.value; }});

        var node = svg.append("g").selectAll(".node")
                .data(nodes)
            .enter().append("g")
                .attr("class", "node")
                .attr("transform", function(d) {{ return "translate(" + d.x + "," + d.y + ")"; }})
            .call(d3.behavior.drag()
                .origin(function(d) {{ return d; }})
                .on("dragstart", function() {{ this.parentNode.appendChild(this); }})
                .on("drag", dragmove));

        node.append("rect")
                .attr("height", function(d) {{ return d.dy; }})
                .attr("width", layout.nodeWidth())
                .style("fill", function(d) {{ return d.color = color(d.name.replace(/ .*/, "")); }})
                .style("stroke", function(d) {{ return d3.rgb(d.color).darker(2); }})
            .append("title")
                .text(function(d) {{ return d.name + "\n" + d.value; }});

        node.append("text")
                .attr("x", -6)
                .attr("y", function(d) {{ return d.dy / 2; }})
                .attr("dy", ".35em")
                .attr("text-anchor", "end")
                .attr("transform", null)
                .text(function(d) {{ return d.name; }})
            .filter(function(d) {{ return d.x < width / 2; }})
                .attr("x", 6 + layout.nodeWidth())
                .attr("text-anchor", "start");

        function dragmove(d) {{
            d3.select(this).attr("transform", "translate(" + d.x + "," + (d.y = Math.max(0, Math.min(height - d.dy, d3.event.y))) + ")");
            layout.relayout();
            link.attr("d", path);
        }}
        </script>
        </body>
        </html>
        """).format(nodes=nodes, links=edges, height=height, width=width)
        return html

    def __str__(self):
        #return self.d3sankey()
        return self.dot()

    def __len__(self):
        return self._len