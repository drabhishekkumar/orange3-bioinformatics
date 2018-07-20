""" GeneNameMatching """
import threading
import numpy as np
import re


from typing import Set, List

from AnyQt.QtWidgets import (
    QSplitter, QTableView,  QHeaderView, QAbstractItemView
)
from AnyQt.QtCore import (
    Qt, QSize, QThreadPool, QSortFilterProxyModel, QAbstractTableModel

)
from AnyQt.QtGui import (
    QFont, QColor
)

from Orange.widgets.gui import (
    vBox, comboBox, ProgressBar, widgetBox, auto_commit, widgetLabel, checkBox,
    rubber, lineEdit, LinkRole, LinkStyledItemDelegate
)
from Orange.widgets.widget import OWWidget
from Orange.widgets.utils import itemmodels
from Orange.widgets.settings import Setting
from Orange.widgets.utils.signals import Output, Input
from Orange.data import StringVariable, DiscreteVariable, Domain, Table, filter as table_filter


from orangecontrib.bioinformatics.widgets.utils.data import (
    TAX_ID, GENE_AS_ATTRIBUTE_NAME, GENE_ID_COLUMN, GENE_ID_ATTRIBUTE
)
from orangecontrib.bioinformatics.widgets.utils.concurrent import Worker
from orangecontrib.bioinformatics.ncbi import taxonomy
from orangecontrib.bioinformatics.ncbi.gene import GeneMatcher, Gene, NCBI_ID, GENE_MATCHER_HEADER, NCBI_DETAIL_LINK


class GeneInfoModel(itemmodels.PyTableModel):
    def __init__(self,  *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.header_labels, self.gene_attributes = GENE_MATCHER_HEADER
        self.setHorizontalHeaderLabels(self.header_labels)

        try:
            # note: make sure ncbi_id is set in GENE_MATCHER_HEADER
            self.entrez_column_index = self.gene_attributes.index('ncbi_id')
        except ValueError as e:
            raise ValueError("Make sure 'ncbi_id' is set in gene.GENE_MATCHER_HEADER")

        self.genes = []

    def initialize(self, list_of_genes):
        self.genes = list_of_genes
        self.__table_from_genes([gene for gene in list_of_genes if gene.ncbi_id])
        self.__set_ncbi_link()

    def data(self, index, role=Qt.DisplayRole):
        # do not alignt text (confilct with LinkStyledItemDelegate)
        if not index.isValid():
            return

        row, column = self.mapToSourceRows(index.row()), index.column()
        role_value = self._roleData.get(row, {}).get(column, {}).get(role)

        if role_value is not None:
            return role_value

        try:
            value = self[row][column]
        except IndexError:
            return

        if role == Qt.DisplayRole:
            return str(value)
        if role == Qt.ToolTipRole:
            return str(value)

    def __set_ncbi_link(self):
        font = QFont()
        font.setUnderline(True)
        color = QColor(Qt.blue)

        for row_index, gene_obj in enumerate(self.genes):
            # note: we expect ncbi_id to be loaded in gene_obj
            link = NCBI_DETAIL_LINK.format(gene_obj.ncbi_id)

            if link:
                self._roleData[row_index][self.entrez_column_index][LinkRole] = link
                self._roleData[row_index][self.entrez_column_index][Qt.FontRole] = font
                self._roleData[row_index][self.entrez_column_index][Qt.ForegroundRole] = color

    def __list_from_gene(self, gene_object):
        # type: (Gene) -> List[str]

        output_list = []
        for tag in self.gene_attributes:
            gene_attr = gene_object.__getattribute__(tag)

            if isinstance(gene_attr, dict):
                # note: db_refs are stored as dicts
                gene_attr = ', '.join('{}: {}'.format(key, val)
                                      for (key, val) in gene_attr.items()) if gene_attr else ' '
            elif isinstance(gene_attr, list):
                # note: synonyms are stored as lists
                gene_attr = ', '.join(gene_attr) if gene_attr else ' '

            output_list.append(gene_attr)
        return output_list

    def __table_from_genes(self, list_of_genes):
        # type: (list) -> None

        table = []
        for gene in list_of_genes:
            gene.load_ncbi_info()
            table.append(self.__list_from_gene(gene))

        self.wrap(table)


class UnknownGeneInfoModel(itemmodels.PyListModel):
    def __init__(self,  *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.header_labels = ['IDs from the input data without corresponding Entrez ID']
        self.genes = []

    def initialize(self, list_of_genes):
        self.genes = list_of_genes
        self.wrap([', '.join([gene.input_name for gene in list_of_genes if not gene.ncbi_id])])

    def data(self, index, role=Qt.DisplayRole):
        row = index.row()
        if role in [self.list_item_role, Qt.EditRole] and self._is_index_valid(index):
            return self[row]
        elif role == Qt.TextAlignmentRole:
            return Qt.AlignLeft | Qt.AlignTop
        elif self._is_index_valid(row):
            return self._other_data[row].get(role, None)

    def headerData(self, section, orientation, role=Qt.DisplayRole):

        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.header_labels[section]
        return QAbstractTableModel.headerData(self, section, orientation, role)


class OWGeneNameMatcher(OWWidget):
    name = "Gene Name Matcher"
    description = "Tool for working with genes"
    icon = "../widgets/icons/OWGeneInfo.svg"
    priority = 5
    want_main_area = True

    use_attr_names = Setting(True)
    selected_organism = Setting(11)

    search_pattern = Setting('')
    gene_as_attr_name = Setting(0)
    exclude_unmatched = Setting(True)
    include_entrez_id = Setting(True)
    replace_id_with_symbol = Setting(True)
    include_gene_info = Setting(False)

    auto_commit = Setting(True)

    class Inputs:
        data_table = Input("Data", Table)

    class Outputs:
        custom_data_table = Output("Data", Table)

    class Information(OWWidget.Information):
        pass

    def sizeHint(self):
        return QSize(1280, 960)

    def __init__(self):
        super().__init__()
        # ATTRIBUTES #

        # input data
        self.input_data = None
        self.input_genes = None
        self.tax_id = None
        self.column_candidates = []
        self.selected_gene_col = None

        # input options
        self.organisms = []

        # gene matcher
        self.gene_matcher = None

        # output data
        self.output_data_table = None
        self.gene_id_column = None
        self.gene_id_attribute = None

        # threads
        self.threadpool = QThreadPool(self)
        self.workers = None

        # progress bar
        self.progress_bar = None

        # filter
        self.filter_labels = ['Unique', 'Partial', 'Unknown']

        # GUI SECTION #

        # Control area
        self.info_box = widgetLabel(
            widgetBox(self.controlArea, "Info", addSpace=True), "Initializing\n"
        )

        organism_box = vBox(self.controlArea, 'Organism')
        self.organism_select_combobox = comboBox(organism_box, self,
                                                 'selected_organism',
                                                 callback=self.on_input_option_change)

        self.get_available_organisms()
        self.organism_select_combobox.setCurrentIndex(self.selected_organism)

        box = widgetBox(self.controlArea, 'Gene IDs in input data')
        self.gene_columns_model = itemmodels.DomainModel(valid_types=(StringVariable, DiscreteVariable))
        self.gene_column_combobox = comboBox(box, self, 'selected_gene_col',
                                             label='Stored in data column',
                                             model=self.gene_columns_model,
                                             sendSelectedValue=True,
                                             callback=self.on_input_option_change)

        self.attr_names_checkbox = checkBox(box, self, 'use_attr_names', 'Stored as feature (column) names',
                                            disables=[(-1, self.gene_column_combobox)],
                                            callback=self.on_input_option_change)

        self.gene_column_combobox.setDisabled(bool(self.use_attr_names))

        output_box = vBox(self.controlArea, 'Output')

        # separator(output_box)
        # output_box.layout().addWidget(horizontal_line())
        # separator(output_box)
        self.exclude_radio = checkBox(output_box, self,
                                      'exclude_unmatched',
                                      'Exclude unmatched genes',
                                      callback=self.on_output_option_change)

        self.replace_radio = checkBox(output_box, self,
                                      'replace_id_with_symbol',
                                      'Replace feature IDs with gene names',
                                      callback=self.on_output_option_change)

        self.include_radio = checkBox(output_box, self,
                                      'include_gene_info',
                                      'Include all other available information',
                                      callback=self.on_output_option_change)

        auto_commit(self.controlArea, self, "auto_commit", "&Commit", box=False)

        rubber(self.controlArea)

        # Main area
        self.filter = lineEdit(self.mainArea, self,
                               'search_pattern', 'Filter:',
                               callbackOnType=True, callback=self.apply_filter)
        # rubber(self.radio_group)
        self.mainArea.layout().addWidget(self.filter)

        # set splitter
        self.splitter = QSplitter()
        self.splitter.setOrientation(Qt.Vertical)

        self.proxy_model = QSortFilterProxyModel()
        self.proxy_model.setFilterKeyColumn(-1)  # note: filter by all columns

        self.table_model = GeneInfoModel()

        self.table_view = QTableView()
        self.table_view.setModel(self.proxy_model)
        self.table_view.viewport().setMouseTracking(True)
        self.table_view.setSortingEnabled(True)
        self.table_view.horizontalHeader().setStretchLastSection(True)
        # self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_view.selectionModel().selectionChanged.connect(self.invalidate)

        self.unknown_model = UnknownGeneInfoModel()

        self.unknown_view = QTableView()
        self.unknown_view.setModel(self.unknown_model)
        self.unknown_view.verticalHeader().hide()
        self.unknown_view.setShowGrid(False)
        self.unknown_view.setSelectionMode(QAbstractItemView.NoSelection)
        self.unknown_view.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        self.splitter.addWidget(self.table_view)
        self.splitter.addWidget(self.unknown_view)

        self.splitter.setStretchFactor(0, 90)
        self.splitter.setStretchFactor(1, 10)

        self.mainArea.layout().addWidget(self.splitter)

    def apply_filter(self):
            self.proxy_model.setFilterRegExp(str(self.search_pattern))

    def __reset_widget_state(self):
        self.Outputs.custom_data_table.send(None)
        self.proxy_model.setSourceModel(None)
        self.table_model.clear()
        self.unknown_model.clear()

    def __selection_changed(self):
        genes = [model_index.data() for model_index in self.extended_view.get_selected_gens()]
        self.extended_view.set_info_model(genes)

    def _update_info_box(self):

        if self.input_genes and self.gene_matcher:
            num_genes = len(self.gene_matcher.genes)
            known_genes = len(self.gene_matcher.get_known_genes())

            info_text = '{} genes in input data\n' \
                        '{} genes match Entrez database\n' \
                        '{} genes with match conflicts\n'.format(num_genes, known_genes, num_genes - known_genes)

        else:
            info_text = 'No genes on input'

        self.info_box.setText(info_text)

    def _progress_advance(self):
        # GUI should be updated in main thread. That's why we are calling advance method here
        if self.progress_bar:
            self.progress_bar.advance()

    def _handle_matcher_results(self):
        assert threading.current_thread() == threading.main_thread()

        if self.progress_bar:
            self.progress_bar.finish()
            self.setStatusMessage('')

        # update info box
        self._update_info_box()

        # set output options
        self.toggle_radio_options()

        # if no known genes, clean up and return
        if not len(self.gene_matcher.get_known_genes()):
            self.__reset_widget_state()
            return

        # set known genes
        self.table_model.initialize(self.gene_matcher.genes)
        self.proxy_model.setSourceModel(self.table_model)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_view.setItemDelegateForColumn(
            self.table_model.entrez_column_index, LinkStyledItemDelegate(self.table_view)
        )

        # set unknown genes
        self.unknown_model.initialize(self.gene_matcher.genes)
        self.unknown_view.resizeRowsToContents()
        self.unknown_view.resizeColumnsToContents()
        self.unknown_view.verticalHeader().setStretchLastSection(True)

        self.on_output_option_change()

    def get_available_organisms(self):
        available_organism = sorted([(tax_id, taxonomy.name(tax_id)) for tax_id in taxonomy.common_taxids()],
                                    key=lambda x: x[1])

        self.organisms = [tax_id[0] for tax_id in available_organism]
        self.organism_select_combobox.addItems([tax_id[1] for tax_id in available_organism])

    def gene_names_from_table(self):
        """ Extract and return gene names from `Orange.data.Table`.
        """
        self.input_genes = []
        if self.input_data:
            if self.use_attr_names:
                self.input_genes = [str(attr.name).strip() for attr in self.input_data.domain.attributes]
            elif self.selected_gene_col:
                if self.selected_gene_col in self.input_data.domain:
                    self.input_genes = [str(e[self.selected_gene_col]) for e in self.input_data
                                        if not np.isnan(e[self.selected_gene_col])]

    def _update_gene_matcher(self):
        self.gene_names_from_table()

        if not self.input_genes:
            self._update_info_box()

        if not self.gene_matcher:
            self.gene_matcher = GeneMatcher(self.get_selected_organism(), case_insensitive=True)

        self.gene_matcher.genes = self.input_genes
        self.gene_matcher.organism = self.get_selected_organism()

    def get_selected_organism(self):
        return self.organisms[self.selected_organism]

    def match_genes(self):
        if self.gene_matcher:
            # init progress bar
            self.progress_bar = ProgressBar(self, iterations=len(self.gene_matcher.genes))
            # status message
            self.setStatusMessage('Gene matcher running')

            worker = Worker(self.gene_matcher.run_matcher, progress_callback=True)
            worker.signals.progress.connect(self._progress_advance)
            worker.signals.finished.connect(self._handle_matcher_results)

            # move download process to worker thread
            self.threadpool.start(worker)

    def on_input_option_change(self):
        self.__reset_widget_state()
        self._update_gene_matcher()
        self.match_genes()

    @Inputs.data_table
    def handle_input(self, data):
        self.__reset_widget_state()
        self.gene_columns_model.set_domain(None)

        if data:
            self.input_data = data

            self.gene_columns_model.set_domain(self.input_data.domain)

            if self.gene_columns_model:
                self.selected_gene_col = self.gene_columns_model[0]

            self.tax_id = str(self.input_data.attributes.get(TAX_ID, ''))
            self.use_attr_names = self.input_data.attributes.get(GENE_AS_ATTRIBUTE_NAME, self.use_attr_names)

            if self.tax_id in self.organisms:
                self.selected_organism = self.organisms.index(self.tax_id)

            self.on_input_option_change()

    @staticmethod
    def get_gene_id_identifier(gene_id_strings):
        # type: (Set[str]) -> str

        if not len(gene_id_strings):
            return NCBI_ID

        regex = re.compile(r'Entrez ID \(.*?\)')
        filtered = filter(regex.search, gene_id_strings)

        return NCBI_ID + ' ({})'.format(len(set(filtered)) + 1)

    def __handle_output_data_table(self, data_table):
        """
        If 'use_attr_names' is True, genes from the input data are in columns.
        """
        # set_of_attributes = set([key for attr in data_table.domain[:] for key in attr.attributes.keys()
        #                          if key == NCBI_ID])
        # gene_id = NCBI_ID if NCBI_ID in data_table.domain or set_of_attributes else None
        if self.exclude_unmatched:
            data_table = self.__filter_unknown(data_table)

        if self.use_attr_names:
            # set_of_attributes = set([key for attr in data_table.domain[:] for key in attr.attributes.keys()
            # if key.startswith(NCBI_ID)])
            # gene_id = self.get_gene_id_identifier(set_of_attributes)
            self.gene_id_attribute = gene_id = NCBI_ID

            for gene in self.gene_matcher.genes:
                if gene.input_name in data_table.domain:

                    if gene.ncbi_id:
                        data_table.domain[gene.input_name].attributes[gene_id] = str(gene.ncbi_id)

                    if self.replace_id_with_symbol:
                        gene.load_ncbi_info()
                        try:
                            data_table.domain[gene.input_name].name = str(gene.symbol)
                        except AttributeError:
                            pass

        else:
            set_of_variables = set([var.name for var in data_table.domain.variables + data_table.domain.metas
                                    if var.name.startswith(NCBI_ID)])

            available_rows, _ = data_table.get_column_view(self.selected_gene_col)
            self.gene_id_column = gene_id = self.get_gene_id_identifier(set_of_variables)

            temp_domain = Domain([], metas=[StringVariable(gene_id)])
            temp_data = [[str(gene.ncbi_id) if gene.ncbi_id else ''] for gene in self.gene_matcher.genes
                         if gene.input_name in available_rows]
            temp_table = Table(temp_domain, temp_data)

            # if columns differ, then concatenate.
            if NCBI_ID in data_table.domain:
                if gene_id != NCBI_ID and not np.array_equal(np.array(temp_data).ravel(),
                                                             data_table.get_column_view(NCBI_ID)[0]):

                    data_table = Table.concatenate([data_table, temp_table])
                else:
                    gene_id = NCBI_ID
            else:
                data_table = Table.concatenate([data_table, temp_table])

        return data_table, gene_id

    def __filter_unknown(self, data_table):

        # data_table, gene_id = self.__handle_output_data_table(data_table)
        # if self.exclude_unmatched:

        known_input_genes = [gene.input_name for gene in self.gene_matcher.get_known_genes()]

        if self.use_attr_names:
            temp_domain = Domain(
                [attr for attr in data_table.domain.attributes if attr.name in known_input_genes],
                metas=data_table.domain.metas,
                class_vars=data_table.domain.class_vars
            )
            data_table = data_table.transform(temp_domain)
        else:

            # create filter from selected column for genes
            only_known = table_filter.FilterStringList(self.selected_gene_col, known_input_genes)
            # apply filter to the data
            data_table = table_filter.Values([only_known])(data_table)

        return data_table

    def commit(self):
        self.Outputs.custom_data_table.send(None)

        if self.output_data_table:
            selection = self.table_view.selectionModel().selectedRows(self.table_model.entrez_column_index)
            selected_genes = [row.data() for row in selection]

            if selected_genes:
                if self.use_attr_names:
                    selected = [column for column in self.output_data_table.domain.attributes
                                if self.gene_id_attribute in column.attributes and
                                str(column.attributes[self.gene_id_attribute]) in selected_genes]

                    domain = Domain(
                        selected, self.output_data_table.domain.class_vars,
                        self.output_data_table.domain.metas
                    )
                    new_data = self.output_data_table.from_table(domain, self.output_data_table)
                    self.Outputs.custom_data_table.send(new_data)

                else:
                    selected_rows = []
                    for row_index, row in enumerate(self.output_data_table):
                        gene_in_row = str(row[self.gene_id_column])

                        if gene_in_row in selected_genes:
                            selected_rows.append(row_index)

                    if selected_rows:
                        selected = self.output_data_table[selected_rows]
                    else:
                        selected = None

                    self.Outputs.custom_data_table.send(selected)
            else:
                self.Outputs.custom_data_table.send(self.output_data_table)

    def toggle_radio_options(self):
        self.replace_radio.setEnabled(bool(self.use_attr_names))

        if self.gene_matcher.genes:
            # enable checkbox if unknown genes are detected
            self.exclude_radio.setEnabled(len(self.gene_matcher.genes) != len(self.gene_matcher.get_known_genes()))
            self.exclude_unmatched = len(self.gene_matcher.genes) != len(self.gene_matcher.get_known_genes())

    def on_output_option_change(self):
        if not self.input_data:
            return

        if not self.use_attr_names and not self.gene_columns_model:
            return

        self.output_data_table = self.input_data.transform(self.input_data.domain.copy())
        self.output_data_table, gene_id = self.__handle_output_data_table(self.output_data_table.copy())

        # handle table attributes
        self.output_data_table.attributes[TAX_ID] = self.get_selected_organism()

        self.output_data_table.attributes[GENE_AS_ATTRIBUTE_NAME] = bool(self.use_attr_names)

        if not bool(self.use_attr_names):
            self.output_data_table.attributes[GENE_ID_COLUMN] = gene_id
        else:
            self.output_data_table.attributes[GENE_ID_ATTRIBUTE] = gene_id

        # print(self.output_data_table)
        self.invalidate()

    def invalidate(self):
        self.commit()

    def on_filter_changed(self):
        self.proxy_model.invalidateFilter()
        self.extended_view.genes_view.resizeRowsToContents()
